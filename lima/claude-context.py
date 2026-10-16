#!/usr/bin/env python3
"""Copy Claude Code project context (memory and session transcripts)
between the host and a wmf-claude Lima VM. bin/claude-vm runs this on the
host, and in the guest through `python3 -c`, so the guest needs no update.
Python 3.9 or newer (the macOS system python3). See docs/lima-vm.md.

  enc PATH                          the project directory name for PATH
  stage SRC OUT [--memory] [--sessions N] [--map FROM TO]...
                                    copy SRC's memory and newest N sessions
                                    into OUT, with paths rewritten
  emit SRC... [--memory] [--sessions N]
                                    the same for each SRC, as JSON lines on
                                    stdout ("w" is the index of the SRC)
  receive OUT --count N [--map FROM TO]...
                                    write JSON lines from stdin into OUT/<w>;
                                    names are checked, paths rewritten
  merge STAGE DEST [--aside DIR] [--no-memory] [--consume] [--old]
        [--norm FROM TO]...         merge a staged copy into the project
                                    directory DEST; --old: a session it
                                    writes is not the newest there
  memdiff STAGED HOST               report how staged memory differs from
                                    the host's, and how to take it

A project directory holds memory/ and, per session, <id>.jsonl and <id>/
(subagents/, tool-results/). Claude Code appends to a resumed session, so
a transcript whose lines start with another's lines extends it: merge takes
the longer one, and leaves a session that both sides changed as it is.
"""
import base64
import json
import os
import re
import sys

UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
NAME = r"[A-Za-z0-9][A-Za-z0-9._-]{0,200}"
# Every name that crosses the boundary matches one of these in full, and
# nothing else.
ALLOWED = re.compile(
    r"memory/" + NAME + r"|sessions/" + UUID + r"\.jsonl"
    r"|sessions/" + UUID + r"/(subagents|tool-results)/" + NAME)
FILE_MAX = 256 << 20
TOTAL_MAX = 4 << 30
# A JSON line holds one file in base64, plus its name.
LINE_MAX = FILE_MAX * 4 // 3 + 4096
# The base of the memory merge: what the last push sent. A dot directory in
# the project directory, which Claude Code does not read as memory.
BASE = ".wmf-claude-base"


def enc(path):
    # As Claude Code names a project directory: every character that is not
    # a letter or a digit becomes "-", and a name over 200 characters keeps
    # 200 and adds "-" and a hash of the path (Java's string hash, base 36).
    name = re.sub(r"[^A-Za-z0-9]", "-", path)
    if len(name) <= 200:
        return name
    h = 0
    for unit in utf16_units(path):
        h = (h * 31 + unit) & 0xffffffff
    h = abs(h - (1 << 32) if h >= 1 << 31 else h)
    digits = ""
    while True:
        h, r = divmod(h, 36)
        digits = "0123456789abcdefghijklmnopqrstuvwxyz"[r] + digits
        if not h:
            break
    return name[:200] + "-" + digits


def utf16_units(text):
    # JavaScript strings count in UTF-16 code units.
    data = text.encode("utf-16-le")
    return [data[i] | data[i + 1] << 8 for i in range(0, len(data), 2)]


def allowed(rel):
    return isinstance(rel, str) and ALLOWED.fullmatch(rel) is not None


def rewriter(maps):
    # A path is replaced only as a whole: /x/core does not match /x/core-old.
    # A full stop that ends a sentence still ends the path: "/x/core." matches,
    # "/x/core.git" does not.
    pats = [(re.compile(re.escape(a.encode()) + rb"(?![A-Za-z0-9_-]|\.[A-Za-z0-9_-])"), b.encode())
            for a, b in sorted(maps, key=lambda m: -len(m[0]))]

    def rewrite(data):
        for pat, to in pats:
            data = pat.sub(lambda _m, to=to: to, data)
        return data
    return rewrite


def write_atomic(path, data):
    # A live session can append to the same directory; never leave half a file.
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.tmp-%d" % (path, os.getpid())
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def read(p):
    with open(p, "rb") as f:
        return f.read()


def content(rel, p):
    # A live session may be writing the last line: send whole lines only. A
    # cut line is still a prefix of the full one, so the merge still sees an
    # extension.
    data = read(p)
    if rel.endswith(".jsonl"):
        data = data[:data.rfind(b"\n") + 1]
    return data


def clean(text):
    # Text the agent wrote goes to the engineer's terminal: no control
    # characters (escape sequences could rewrite the screen).
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "", text)


def select(src, memory, sessions):
    """Yield (name, absolute path) for what to copy out of project dir src."""
    if memory and os.path.isdir(os.path.join(src, "memory")):
        for n in sorted(os.listdir(os.path.join(src, "memory"))):
            p = os.path.join(src, "memory", n)
            if os.path.isfile(p) and not os.path.islink(p) and allowed("memory/" + n):
                yield "memory/" + n, p
    if sessions > 0 and os.path.isdir(src):
        found = [n for n in os.listdir(src)
                 if re.fullmatch(UUID + r"\.jsonl", n) and os.path.isfile(os.path.join(src, n))]
        found.sort(key=lambda n: os.path.getmtime(os.path.join(src, n)), reverse=True)
        for n in found[:sessions]:
            yield "sessions/" + n, os.path.join(src, n)
            sid = n[:-len(".jsonl")]
            for sub in ("subagents", "tool-results"):
                d = os.path.join(src, sid, sub)
                if not os.path.isdir(d):
                    continue
                for f in sorted(os.listdir(d)):
                    rel = "sessions/%s/%s/%s" % (sid, sub, f)
                    p = os.path.join(d, f)
                    if os.path.isfile(p) and not os.path.islink(p) and allowed(rel):
                        yield rel, p


def cmd_stage(src, out, memory, sessions, maps):
    rewrite = rewriter(maps)
    n = 0
    for rel, p in select(src, memory, sessions):
        write_atomic(os.path.join(out, rel), rewrite(content(rel, p)))
        n += 1
    print("staged %d file%s" % (n, "" if n == 1 else "s"))


def cmd_emit(srcs, memory, sessions):
    out = sys.stdout
    for w, src in enumerate(srcs):
        for rel, p in select(src, memory, sessions):
            data = content(rel, p)
            if len(data) <= FILE_MAX:
                out.write(json.dumps({"w": w, "p": rel, "b": base64.b64encode(data).decode()}) + "\n")
    out.flush()


def cmd_receive(out, count, maps):
    # This is the trust boundary on the host: the lines come from the agent.
    # Each read is bounded, so a line never grows past one file's size.
    rewrite = rewriter(maps)
    root = os.path.realpath(out)
    seen, total, bad, n = set(), 0, 0, 0
    stdin = sys.stdin.buffer
    while True:
        line = stdin.readline(LINE_MAX)
        if not line:
            break
        if not line.endswith(b"\n") and len(line) >= LINE_MAX:
            while True:  # too long: drop the rest of the line
                rest = stdin.readline(LINE_MAX)
                if not rest or rest.endswith(b"\n"):
                    break
            bad += 1
            continue
        try:
            rec = json.loads(line)
            w, rel, b64 = rec["w"], rec["p"], rec["b"]
            if (type(w) is not int or not 0 <= w < count or not allowed(rel)
                    or not isinstance(b64, str) or (w, rel) in seen):
                raise ValueError(rel)
            data = base64.b64decode(b64, validate=True)
            if len(data) > FILE_MAX or total + len(data) > TOTAL_MAX:
                raise ValueError("too large")
            dest = os.path.realpath(os.path.join(root, str(w), rel))
            if not dest.startswith(root + os.sep):
                raise ValueError(rel)
        except Exception:  # anything the agent sends, deep nesting included
            bad += 1
            continue
        seen.add((w, rel))
        total += len(data)
        write_atomic(dest, rewrite(data))
        n += 1
    print("received %d file%s%s" % (n, "" if n == 1 else "s",
                                     "; refused %d" % bad if bad else ""), file=sys.stderr)
    return 1 if bad else 0


def first_prompt(data):
    # The transcript may come from the agent: any line can be any JSON.
    for line in data.splitlines():
        try:
            d = json.loads(line)
        except Exception:  # deep nesting raises RecursionError
            continue
        if not isinstance(d, dict) or d.get("type") != "user" or not isinstance(d.get("message"), dict):
            continue
        c = d["message"].get("content")
        if isinstance(c, list):
            c = " ".join(x["text"] for x in c
                         if isinstance(x, dict) and x.get("type") == "text" and isinstance(x.get("text"), str))
        if isinstance(c, str) and c.strip() and not c.startswith("<"):
            c = " ".join(clean(c).split())
            return c[:60] + ("..." if len(c) > 60 else "")
    return ""


def merge_transcript(new, dest, norm):
    """Return (action, data to write or None). Lines are compared after norm,
    so a path the rewrite could not tell apart does not count as a change."""
    if not os.path.exists(dest):
        return "added", new
    old = read(dest)
    if old == new:
        return "same", None
    ol, nl = old.splitlines(True), new.splitlines(True)
    nol, nnl = [norm(l) for l in ol], [norm(l) for l in nl]
    if nnl[:len(nol)] == nol:
        if len(nl) == len(ol):
            return "same", None
        # Keep this side's lines as they are; add the other side's new ones.
        return "extended", old + b"".join(nl[len(ol):])
    if nol[:len(nnl)] == nnl:
        return "same", None  # this side is ahead
    return "conflict", None


def merge_index(new, old, base):
    """MEMORY.md, three ways: the incoming lines without the ones this side
    removed since the base, then the lines only this side added."""
    nl, ol = new.splitlines(), old.splitlines()
    bs, os_, ns = set((base or b"").splitlines()), set(ol), set(nl)
    removed_here = bs - os_
    out = [l for l in nl if l not in removed_here] + [l for l in ol if l not in bs and l not in ns]
    return b"".join(l + b"\n" for l in out)


def cmd_merge(stage, dest, aside, memory, consume, norm, old):
    report = []
    mem = os.path.join(stage, "memory")
    if memory and os.path.isdir(mem):
        added, updated, kept, moved = [], [], [], []
        for n in sorted(os.listdir(mem)):
            new = read(os.path.join(mem, n))
            target = os.path.join(dest, "memory", n)
            basep = os.path.join(dest, BASE, "memory", n)
            base = read(basep) if os.path.exists(basep) else None
            if not os.path.exists(target):
                write_atomic(target, new)
                added.append(n)
            else:
                old = read(target)
                if old == new:
                    pass
                elif n == "MEMORY.md":
                    write_atomic(target, merge_index(new, old, base))
                    updated.append(n)
                elif base is not None and old == base:
                    write_atomic(target, new)  # only the other side changed it
                    updated.append(n)
                elif base is not None and new == base:
                    kept.append(n)  # only this side changed it
                else:
                    # Both sides changed it, or there is no base: the incoming
                    # copy wins, and this side's goes aside, out of memory/.
                    if aside:
                        write_atomic(os.path.join(aside, n), old)
                    write_atomic(target, new)
                    moved.append(n)
            write_atomic(basep, new)
        for label, group in (("new", added), ("updated", updated), ("kept as changed here", kept)):
            if group:
                report.append("memory: %s: %s" % (label, ", ".join(group)))
        if moved:
            report.append("memory: changed on both sides, so replaced; the old copies are in %s: %s"
                          % (aside, ", ".join(moved)))
    ses = os.path.join(stage, "sessions")
    if os.path.isdir(ses):
        # --old: a written session gets a file time before every other
        # session here, so `claude --continue` (the newest file) does not
        # pick it up by itself; /resume still lists it.
        others = [os.path.getmtime(os.path.join(dest, n)) for n in
                  (os.listdir(dest) if os.path.isdir(dest) else []) if n.endswith(".jsonl")]
        oldest = min(others) - 1 if (old and others) else None
        for n in sorted(os.listdir(ses)):
            p = os.path.join(ses, n)
            if not n.endswith(".jsonl") or not os.path.isfile(p):
                continue
            sid = n[:-len(".jsonl")]
            side_conflicts = []
            try:
                new = read(p)
                action, data = merge_transcript(new, os.path.join(dest, n), norm)
                prompt = first_prompt(new)
                if action == "conflict":
                    # Leave the whole session as it is, side files too.
                    report.append("session %s left as it is: both sides changed it  %s" % (sid[:8], prompt))
                    continue
                if data is not None:
                    write_atomic(os.path.join(dest, n), data)
                    if oldest is not None:
                        os.utime(os.path.join(dest, n), (oldest, oldest))
                for root, _dirs, files in os.walk(os.path.join(ses, sid)):
                    for f in sorted(files):
                        rel = os.path.relpath(os.path.join(root, f), ses)
                        a, d = merge_transcript(read(os.path.join(root, f)), os.path.join(dest, rel), norm)
                        if d is not None:
                            write_atomic(os.path.join(dest, rel), d)
                        elif a == "conflict":
                            side_conflicts.append(f)
            except Exception as e:  # anything in an agent-written transcript
                report.append("session %s could not be merged: %s" % (sid[:8], e.__class__.__name__))
                continue
            if action != "same":
                report.append("session %s %s  %s" % (sid[:8], action, prompt))
            if side_conflicts:
                report.append("session %s: these files differ on both sides and were left as they are: %s"
                              % (sid[:8], ", ".join(clean(f) for f in side_conflicts)))
                continue  # a consumed stage keeps the session
            if consume:
                os.remove(p)
                for root, dirs, files in os.walk(os.path.join(ses, sid), topdown=False):
                    for f in files:
                        os.remove(os.path.join(root, f))
                    for d in dirs:
                        os.rmdir(os.path.join(root, d))
                if os.path.isdir(os.path.join(ses, sid)):
                    os.rmdir(os.path.join(ses, sid))
        if consume and not os.listdir(ses):
            os.rmdir(ses)
    print("\n".join(report) if report else "nothing new")


def cmd_memdiff(staged, host):
    names = sorted(os.listdir(staged)) if os.path.isdir(staged) else []
    new, changed, index = [], [], []
    for n in names:
        s, h = os.path.join(staged, n), os.path.join(host, n)
        if not os.path.exists(h):
            new.append(n)
        elif read(s) != read(h):
            if n == "MEMORY.md":
                have = set(read(h).splitlines())
                index = [clean(l.decode("utf-8", "replace")) for l in read(s).splitlines() if l not in have]
            else:
                changed.append(n)
    if not (new or changed or index):
        print("    The VM's memory has nothing that this machine lacks.")
        return
    q = lambda p: "'" + p.replace("'", "'\\''") + "'"
    for label, group in (("only in the VM", new), ("different in the VM", changed)):
        if group:
            print("    %s: %s" % (label, ", ".join(clean(n) for n in group)))
    if changed:
        print("    Compare: diff -ru %s %s" % (q(host), q(staged)))
    if new or changed:
        print("    Take them: mkdir -p %s && cp %s %s/"
              % (q(host), " ".join(q(os.path.join(staged, n)) for n in new + changed), q(host)))
    if index:
        print("    Lines in the VM's MEMORY.md that this machine's lacks:")
        for l in index:
            print("      " + l)
        print("    Append them: grep -vxFf %s %s >> %s"
              % (q(os.path.join(host, "MEMORY.md")), q(os.path.join(staged, "MEMORY.md")), q(os.path.join(host, "MEMORY.md"))))


def main(argv):
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    cmd, rest = argv[0], argv[1:]
    maps, norms, flags, pos = [], [], {}, []
    i = 0
    while i < len(rest):
        a = rest[i]
        if a in ("--map", "--norm"):
            (maps if a == "--map" else norms).append((rest[i + 1], rest[i + 2]))
            i += 3
            continue
        if a in ("--sessions", "--aside", "--count"):
            flags[a] = rest[i + 1]
            i += 2
            continue
        if a in ("--memory", "--no-memory", "--consume", "--old"):
            flags[a] = True
        else:
            pos.append(a)
        i += 1
    sessions = int(flags.get("--sessions", 0))
    if cmd == "enc":
        print(enc(pos[0]))
    elif cmd == "stage":
        cmd_stage(pos[0], pos[1], "--memory" in flags, sessions, maps)
    elif cmd == "emit":
        cmd_emit(pos, "--memory" in flags, sessions)
    elif cmd == "receive":
        return cmd_receive(pos[0], int(flags.get("--count", 1)), maps)
    elif cmd == "merge":
        cmd_merge(pos[0], pos[1], flags.get("--aside"), "--no-memory" not in flags,
                  "--consume" in flags, rewriter(norms), "--old" in flags)
    elif cmd == "memdiff":
        cmd_memdiff(pos[0], pos[1])
    else:
        print(__doc__, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
