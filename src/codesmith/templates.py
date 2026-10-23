"""Workflow templates and `runner new` (docs/03-workflow-file.md, Templates).

A template is a workflow file with a `[template]` table (a description and the parameters) and
three task keys of its own (`for_each`, `chain`, `note`). `runner new` renders it into an ordinary
workflow file. The engine never sees a template: nothing is written unless the result loads with
no error through the ordinary loader.

- One pass over the template's string values. A parameter value is never scanned again, so a value
  holding `{param.x}`, quotes or newlines comes out literally.
- A string that is exactly one placeholder takes the value with its type; in an array, an element
  that is exactly a list placeholder is spliced. Anywhere else a list is an error.
- Only `{param.NAME}`, `{each.n}`, `{each.value}` and `{last.NAME}` are placeholders. Any other
  brace text (`${HOME}`, `{a,b}`, C++) is literal.
"""

import datetime
import hashlib
import math
import os
import re
import subprocess
import tempfile
import tomllib

from . import workflow

PLACEHOLDER_RE = re.compile(r"\{((?:param|last)\.[A-Za-z_][A-Za-z0-9_]*|each\.n|each\.value)\}")
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")
PARAM_TYPES = ("string", "integer", "list")
TEMPLATE_KEYS = {"description", "params"}
PARAM_SPEC_KEYS = {"required", "default", "type", "description"}
TASK_ONLY_KEYS = ("for_each", "chain", "note")
BARE_KEY_RE = re.compile(r"[A-Za-z0-9_-]+\Z")


class TemplateError(Exception):
    """Raised with every problem found; nothing has been written."""

    def __init__(self, errors, warnings=()):
        super().__init__("\n".join(errors))
        self.errors = list(errors)
        self.warnings = list(warnings)


# -- finding templates ----------------------------------------------------------------------------

def search_dirs(library_dirs=()):
    """The project's libraries in the order given (earlier wins), then the built-in one: the same
    order as types and personas."""
    return [os.path.realpath(d) for d in library_dirs] + [os.path.realpath(workflow.BUILTIN_LIBRARY)]


def find(name, library_dirs=()):
    if not NAME_RE.match(name):
        raise TemplateError([f"'{name}' is not a template name"])
    for d in search_dirs(library_dirs):
        path = os.path.join(d, "workflows", name + ".toml")
        if os.path.isfile(path):
            return path
    raise TemplateError([f"template '{name}' not found in "
                         + ", ".join(os.path.join(d, "workflows") for d in search_dirs(library_dirs))])


def available(library_dirs=()):
    """[(name, description, path)] of every template, shadowed ones left out, sorted by name."""
    found = {}
    for d in search_dirs(library_dirs):
        folder = os.path.join(d, "workflows")
        if not os.path.isdir(folder):
            continue
        for fname in sorted(os.listdir(folder)):
            if fname.endswith(".toml") and fname[:-5] not in found:
                path = os.path.join(folder, fname)
                try:
                    description = load(path).description
                except TemplateError as exc:
                    description = "(does not load: " + exc.errors[0] + ")"
                found[fname[:-5]] = (fname[:-5], description, path)
    return [found[n] for n in sorted(found)]


# -- loading a template ---------------------------------------------------------------------------

class Template:
    def __init__(self, path, name, raw, data, sha256):
        self.path, self.name, self.raw, self.data, self.sha256 = path, name, raw, data, sha256
        self.description = ""
        self.params = {}            # name -> {"type", "required", "default", "description"}


def load(path):
    """Parse and check a template. Raises TemplateError with every problem found."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        raise TemplateError([f"{path}: cannot read: {exc.strerror or exc}"])
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise TemplateError([f"{path}: not valid TOML: {exc}"])
    name = os.path.splitext(os.path.basename(path))[0]
    t = Template(path, name, raw, data, hashlib.sha256(raw).hexdigest())
    errors = []
    head = data.pop("template", None)
    if not isinstance(head, dict):
        raise TemplateError([f"{path}: a template needs a [template] table"])
    for key in head:
        if key not in TEMPLATE_KEYS:
            errors.append(f"{path} [template]: unknown key '{key}'")
    t.description = head.get("description", "")
    if not isinstance(t.description, str) or not t.description.strip():
        errors.append(f"{path} [template]: 'description' is required")
    specs = head.get("params", {})
    if not isinstance(specs, dict):
        errors.append(f"{path} [template]: 'params' must be a table of tables")
        specs = {}
    for pname, spec in specs.items():
        where = f"{path} [template.params.{pname}]"
        if not re.match(r"[A-Za-z_][A-Za-z0-9_]*\Z", pname):
            errors.append(f"{where}: a parameter name is letters, digits and '_'")
            continue
        if not isinstance(spec, dict):
            errors.append(f"{where}: must be a table")
            continue
        errors += [f"{where}: unknown key '{k}'" for k in spec if k not in PARAM_SPEC_KEYS]
        required = spec.get("required", False)
        if not isinstance(required, bool):
            errors.append(f"{where}: 'required' must be a boolean")
        if required and "default" in spec:
            errors.append(f"{where}: a required parameter cannot have a default")
        elif not required and "default" not in spec:
            errors.append(f"{where}: needs 'default', or 'required = true'")
        ptype = spec.get("type") or (_type_of(spec["default"]) if "default" in spec else "string")
        if ptype not in PARAM_TYPES:
            errors.append(f"{where}: 'type' must be \"string\", \"integer\" or \"list\"")
        elif "default" in spec and _type_of(spec["default"]) != ptype:
            errors.append(f"{where}: the default is not a {ptype}")
        elif isinstance(spec.get("default"), list):
            errors += [f"{where}: the default's {p}" for p in _bad_elements(spec["default"])]
        description = spec.get("description", "")
        if not isinstance(description, str):
            errors.append(f"{where}: 'description' must be a string")
        t.params[pname] = {"type": ptype, "required": required is True,
                           "default": spec.get("default"), "description": description}
    for key in ("root", "library"):
        if key in data:
            errors.append(f"{path}: a template may not set '{key}'; `runner new` writes it")
    errors += _check_placeholders(t)
    if errors:
        raise TemplateError(errors)
    return t


def _type_of(value):
    if isinstance(value, str):
        return "string"
    if isinstance(value, int) and not isinstance(value, bool):
        return "integer"
    if isinstance(value, list):
        return "list"
    return type(value).__name__


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)


def _check_placeholders(t):
    """Static checks over the whole template, so an empty `for_each` hides nothing."""
    errors, used = [], set()
    tasks = t.data.get("task", [])
    tasks = tasks if isinstance(tasks, list) else []
    iterated = {task.get("for_each") for task in tasks if isinstance(task, dict)}
    for key, value in t.data.items():
        if key == "task" and isinstance(value, list):
            continue
        for s in _strings(value):
            for name in PLACEHOLDER_RE.findall(s):
                if name.startswith("each."):
                    errors.append(f"{t.path}: '{{{name}}}' is used outside a for_each task")
                used.add(name)
    for n, task in enumerate(tasks, 1):
        if not isinstance(task, dict):
            continue
        where = f"{t.path}: task '{task.get('id', '#' + str(n))}'"
        each = task.get("for_each")
        if each is not None:
            if not isinstance(each, str) or each not in t.params:
                errors.append(f"{where}: 'for_each' must name a declared list parameter")
            elif t.params[each]["type"] != "list":
                errors.append(f"{where}: 'for_each' names '{each}', which is not a list parameter")
            used.add("param." + str(each))
        if "chain" in task and not isinstance(task["chain"], bool):
            errors.append(f"{where}: 'chain' must be a boolean")
        if task.get("chain") and each is None:
            errors.append(f"{where}: 'chain' needs 'for_each'")
        if "note" in task and not isinstance(task["note"], str):
            errors.append(f"{where}: 'note' must be a string")
        for s in _strings(task):
            for name in PLACEHOLDER_RE.findall(s):
                used.add(name)
                if name.startswith("each.") and each is None:
                    errors.append(f"{where}: '{{{name}}}' is used outside a for_each task")
    for name in sorted(used):
        kind, _, pname = name.partition(".")
        if kind == "param" and pname not in t.params:
            errors.append(f"{t.path}: uses undeclared parameter '{{{name}}}'")
        if kind == "last" and pname not in iterated:
            errors.append(f"{t.path}: '{{{name}}}' names no for_each list")
    for pname in t.params:
        if "param." + pname not in used and "last." + pname not in used:
            errors.append(f"{t.path}: parameter '{pname}' is declared and never used")
    return errors


# -- parameters -----------------------------------------------------------------------------------

def read_params(path):
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except OSError as exc:
        raise TemplateError([f"{path}: cannot read: {exc.strerror or exc}"])
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise TemplateError([f"{path}: not valid TOML: {exc}"])


def _bad_elements(value):
    """The problems with a list value's elements: each is a string, a number or a table (an inline
    table such as a reviewer entry); a boolean, a date or a nested list is not a list element."""
    return [f"element {n} is a {_type_of(v)}; a list holds strings, numbers or tables"
            for n, v in enumerate(value, 1)
            if isinstance(v, bool) or not isinstance(v, (str, int, float, dict))]


def resolve_params(t, given=None, sets=()):
    """The value of every parameter: `--set NAME=VALUE` over the params file over the defaults.
    Every problem is reported at once, each naming the parameter."""
    errors, values = [], {}
    given = dict(given or {})
    for item in sets:
        name, eq, text = item.partition("=")
        name = name.strip()
        try:
            item.encode("utf-8")
        except UnicodeEncodeError:
            shown = item.encode("utf-8", "backslashreplace").decode("ascii", "replace")
            errors.append(f"--set '{shown}': not valid UTF-8")
            continue
        if not eq:
            errors.append(f"--set '{item}': expected NAME=VALUE")
        elif name not in t.params:
            errors.append(f"--set: template '{t.name}' has no parameter '{name}'")
        elif t.params[name]["type"] == "list":
            errors.append(f"--set: parameter '{name}' is a list; give it in --params")
        elif t.params[name]["type"] == "integer":
            try:
                given[name] = int(text)
            except ValueError:
                errors.append(f"--set: parameter '{name}' must be an integer, got '{text}'")
        else:
            given[name] = text
    for name, value in given.items():
        if name not in t.params:
            errors.append(f"template '{t.name}' has no parameter '{name}'")
        elif _type_of(value) != t.params[name]["type"]:
            errors.append(f"parameter '{name}' must be a {t.params[name]['type']}, "
                          f"got a {_type_of(value)}")
        elif isinstance(value, list):
            errors += [f"parameter '{name}': {p}" for p in _bad_elements(value)]
    for name, spec in t.params.items():
        if name in given:
            values[name] = given[name]
        elif spec["required"]:
            errors.append(f"missing required parameter '{name}'")
        else:
            values[name] = spec["default"]
    if errors:
        raise TemplateError(errors)
    return values


# -- substitution ---------------------------------------------------------------------------------

class _Missing(Exception):
    pass


def _lookup(name, ctx):
    if name not in ctx:
        raise _Missing(name)
    return ctx[name]


def substitute(value, ctx, where, errors):
    """One pass over the string values of `value`; `ctx` maps placeholder names to values."""
    if isinstance(value, str):
        whole = PLACEHOLDER_RE.fullmatch(value)
        try:
            if whole:
                return _lookup(whole.group(1), ctx)

            def one(match):
                found = _lookup(match.group(1), ctx)
                if isinstance(found, (list, dict)):
                    raise TypeError(match.group(1))
                return str(found)
            return PLACEHOLDER_RE.sub(one, value)
        except _Missing as exc:
            errors.append(f"{where}: '{{{exc.args[0]}}}' has no value"
                          + (" (its for_each list is empty)" if exc.args[0].startswith("last.") else ""))
        except TypeError as exc:
            errors.append(f"{where}: list '{{{exc.args[0]}}}' is used inside a longer string; a list "
                          "can only be a whole value or an array element")
        return value
    if isinstance(value, list):
        out = []
        for v in value:
            whole = PLACEHOLDER_RE.fullmatch(v) if isinstance(v, str) else None
            if whole and isinstance(ctx.get(whole.group(1)), list):
                out.extend(ctx[whole.group(1)])         # spliced, never rescanned
            else:
                out.append(substitute(v, ctx, where, errors))
        return out
    if isinstance(value, dict):
        return {k: substitute(v, ctx, where, errors) for k, v in value.items()}
    return value


def expand(t, values):
    """(document, notes, origins): the rendered workflow as data, the comment to write above each
    task (by position), and for each rendered task id the template task and copy it came from."""
    errors = []
    ctx = {"param." + k: v for k, v in values.items()}
    tasks = t.data.get("task", [])
    tasks = tasks if isinstance(tasks, list) else []
    # `{last.NAME}` is known before any task is rendered, so a task may name it wherever it is.
    for task in tasks:
        each = task.get("for_each") if isinstance(task, dict) else None
        if each and values.get(each):
            k = len(values[each])
            ctx["last." + each] = substitute(task.get("id", ""), dict(ctx, **{
                "each.n": k, "each.value": values[each][-1]}), f"task '{task.get('id')}'", errors)
    doc = {}
    for key, value in t.data.items():
        if key != "task":
            doc[key] = substitute(value, ctx, f"'{key}'", errors)
    out, notes, origins = [], [], {}
    for task in tasks:
        if not isinstance(task, dict):
            out.append(task)
            notes.append("")
            continue
        body = {k: v for k, v in task.items() if k not in TASK_ONLY_KEYS}
        each = task.get("for_each")
        copies = [(None, ctx)] if each is None else [
            (n, dict(ctx, **{"each.n": n, "each.value": v})) for n, v in enumerate(values[each], 1)]
        previous = None
        for n, local in copies:
            where = f"template task '{task.get('id')}'" + (f", copy {n}" if n else "")
            copy = substitute(body, local, where, errors)
            if task.get("chain") and previous is not None:
                needs = copy.get("needs", [])
                copy["needs"] = (needs if isinstance(needs, list) else [needs]) + [previous]
            previous = copy.get("id")
            note = substitute(task.get("note", ""), local, where, errors)
            out.append(copy)
            notes.append(note)
            if isinstance(copy.get("id"), str):
                origins[copy["id"]] = (task.get("id"), n)
    if "task" in t.data or out:
        doc["task"] = out
    if errors:
        raise TemplateError(errors)
    return doc, notes, origins


# -- the TOML writer ------------------------------------------------------------------------------
# The standard library reads TOML but cannot write it. This writes the subset a workflow uses:
# strings, integers, floats, booleans, arrays, inline tables, tables and arrays of tables.

def _key(k):
    return k if BARE_KEY_RE.match(k) else _string(k)


def _string(s, multiline=False):
    if multiline and "\n" in s and not re.search(r"[\x00-\x08\x0b-\x1f\x7f]", s):
        body = s.replace("\\", "\\\\").replace('"', '\\"').replace("\r", "\\r")
        return '"""\n' + body + '"""'
    out = []
    for ch in s:
        if ch in '"\\':
            out.append("\\" + ch)
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif ord(ch) < 0x20 or ord(ch) == 0x7f:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _comment(text):
    """A line of a note as comment text: TOML allows no control character but tab in a comment."""
    return re.sub(r"[\x00-\x08\x0a-\x1f\x7f]", lambda m: f"\\u{ord(m.group()):04x}", text)


def inline(value, multiline=False):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
        return repr(value)
    if isinstance(value, str):
        return _string(value, multiline)
    if isinstance(value, list):
        return "[" + ", ".join(inline(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{_key(k)} = {inline(v)}" for k, v in value.items()) + " }" \
            if value else "{}"
    raise TemplateError([f"cannot write a {type(value).__name__} value as TOML"])


def _is_table_array(value):
    return isinstance(value, list) and value and all(isinstance(v, dict) for v in value)


def dumps(doc, notes=(), header=""):
    """`doc` as TOML. Top-level tables become [sections]; an array of tables becomes [[sections]]
    whose own nested values are written inline. `notes[i]` is a comment above the i-th entry of
    the top-level array of tables."""
    lines = [header.rstrip("\n"), ""] if header else []

    def section(path, table):
        for k, v in table.items():
            if not isinstance(v, dict) and not (not path and _is_table_array(v)):
                lines.append(f"{_key(k)} = {inline(v, multiline=True)}")
        for k, v in table.items():
            if isinstance(v, dict):
                lines.append("")
                lines.append("[" + ".".join(_key(p) for p in path + [k]) + "]")
                section(path + [k], v)
        if not path:
            for k, v in table.items():
                if _is_table_array(v):
                    for n, entry in enumerate(v):
                        lines.append("")
                        note = notes[n] if k == "task" and n < len(notes) else ""
                        lines.extend("# " + _comment(line) if line else "#" for line in note.splitlines())
                        lines.append(f"[[{_key(k)}]]")
                        for ek, ev in entry.items():
                            lines.append(f"{_key(ek)} = {inline(ev, multiline=True)}")

    section([], doc)
    return "\n".join(lines) + "\n"


# -- rendering and writing ------------------------------------------------------------------------

def _git_top(directory):
    try:
        res = subprocess.run(["git", "-C", directory, "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True)
    except OSError:
        return None
    return res.stdout.strip() if res.returncode == 0 and res.stdout.strip() else None


def _shown(t, libraries, out_dir):
    """The template's path for the generated file's header: relative to its library, never an
    absolute path (it would name the owner's home directory in a committed file)."""
    path = os.path.realpath(t.path)
    library = os.path.dirname(os.path.dirname(path))
    name = os.path.basename(path)
    if library == os.path.realpath(workflow.BUILTIN_LIBRARY):
        return f"the built-in library/workflows/{name}"
    if library in libraries:
        return os.path.relpath(path, out_dir).replace(os.sep, "/")
    return f"workflows/{name} of a library not passed to `runner new`"


def render(t, values, out_dir, root=None, library_dirs=(), today=None, default_name=None):
    """The text of the generated workflow, to be written in `out_dir`. `root` defaults to the git
    top level holding `out_dir`; `library_dirs` (the ones the template was looked up in) are
    written as the workflow's `library`, so project types and personas resolve the same way. A
    template without a top-level `name` gets `default_name` (the output file's stem).

    Every directory is resolved through symbolic links first: git reports the top level resolved
    (/private/tmp, not /tmp), and a relative path between a resolved and an unresolved one climbs
    to the file-system root."""
    doc, notes, origins = expand(t, values)
    out_dir = os.path.realpath(out_dir)
    root = os.path.realpath(root) if root else _git_top(out_dir)
    root = os.path.realpath(root) if root else root
    first = {"name": doc.pop("name")} if "name" in doc else {}
    if not first and default_name:
        first["name"] = default_name
    if root:
        first["root"] = os.path.relpath(root, out_dir).replace(os.sep, "/")
    libraries = [os.path.realpath(d) for d in library_dirs]
    if libraries:
        first["library"] = [os.path.relpath(d, out_dir).replace(os.sep, "/") for d in libraries]
    doc = {**first, **doc}
    shown = _shown(t, libraries, out_dir)
    header = [f"# Generated by `runner new {t.name}` from {shown} (sha256 {t.sha256})",
              f"# on {(today or datetime.date.today()).isoformat()} with:"]
    header += [f"#   {name} = {inline(value)}" for name, value in values.items()] or ["#   (no parameters)"]
    header.append("# Edit freely: this file, not the template, defines the work.")
    return dumps(doc, notes, "\n".join(header)), origins


def _trace(message, origins, temp_name, out_name):
    """Name the template task and copy a loader error came from."""
    message = message.replace(temp_name, out_name)
    for tid in re.findall(r"'([A-Za-z0-9][A-Za-z0-9_.-]*)'", message):
        origin = origins.get(tid.split(".review.")[0])
        if origin and (origin[0] != tid or origin[1]):
            return message + f" (from template task '{origin[0]}'" + \
                (f", copy {origin[1]})" if origin[1] else ")")
    return message


def write(t, values, output, force=False, root=None, library_dirs=()):
    """Render, load the result through the ordinary loader from a temporary file beside `output`,
    and move it into place only if it loads with no error. Returns the loaded workflow."""
    output = os.path.abspath(output)
    out_dir = os.path.dirname(output)
    if os.path.exists(output) and not force:
        raise TemplateError([f"{output} exists; nothing was written (pass --force to replace it)"])
    if not os.path.isdir(out_dir):
        raise TemplateError([f"directory {out_dir} not found; nothing was written"])
    text, origins = render(t, values, out_dir, root, library_dirs,
                           default_name=os.path.splitext(os.path.basename(output))[0])
    fd, temp = tempfile.mkstemp(dir=out_dir, prefix="." + os.path.basename(output) + ".",
                                suffix=".toml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        mask = os.umask(0)                      # mkstemp makes the file 0600; give it the usual mode
        os.umask(mask)
        os.chmod(temp, 0o666 & ~mask)
        wf = workflow.load(temp)
        temp_name, out_name = os.path.basename(temp), os.path.basename(output)
        # A template's `protected` is a promise its parameters must not break: a literal
        # path in `outputs` or `writes` would lift it, so that combination is refused.
        wf.errors.extend(f"task '{tid}': {key} path '{path}' would lift the template's protection "
                         f"'{pattern}'; choose parameters that keep it apart"
                         for tid, key, path, pattern in wf.protection_overrides)
        if wf.errors:
            raise TemplateError([_trace(e, origins, temp_name, out_name) for e in wf.errors],
                                [_trace(w, origins, temp_name, out_name) for w in wf.warnings])
        wf.warnings = [_trace(w, origins, temp_name, out_name) for w in wf.warnings]
        os.replace(temp, output)
        wf.workflow_file = output
        return wf
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def describe(t):
    lines = [f"{t.name}: {t.description}", f"from {t.path}", ""]
    width = max([len(n) for n in t.params] + [4])
    for name, spec in t.params.items():
        state = "required" if spec["required"] else "default " + inline(spec["default"])
        lines.append(f"  {name:<{width}}  {spec['type']:<7}  {state}")
        if spec["description"]:
            lines.append(f"  {'':<{width}}  {spec['description']}")
    return "\n".join(lines)

