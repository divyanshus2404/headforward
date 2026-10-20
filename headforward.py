#!/usr/bin/env python3
"""headforward: cut the tokens Claude, ChatGPT and coding agents spend.

Single file, zero dependencies, Python 3.9+. Install `tiktoken` for exact counts.

Coding agents (biggest savings: compresses tool output, JSON, logs, file reads):
  headforward wrap claude            # Claude Code
  headforward wrap codex             # OpenAI Codex CLI (API key auth)
  headforward proxy --port 8787      # any app or SDK: set its base URL
  headforward stats                  # tokens saved so far

Prompts:
  headforward prompt.txt -l high
  pbpaste | headforward - -l high | pbcopy
  headforward api.json --tool        # tool output: JSON tables, log repeats

Prompt levels:
  light   whitespace and blank lines
  medium  + filler words, wordy phrases, greetings and sign-offs, repeated sentences
  high    + articles, hedges, markdown emphasis, minify JSON code blocks
  max     + strip comment-only lines and blank lines inside code blocks
"""
import argparse
import datetime
import difflib
import hashlib
import http.client
import http.server
import json
import os
import re
import shlex
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

LEVELS = ["light", "medium", "high", "max"]

CODE = re.compile(r"(```[^\n]*\n.*?```|`[^`\n]+`)", re.S)

# (pattern, replacement), applied at medium and above. Case-insensitive.
PHRASES = [
    (r"\bin order to\b", "to"),
    (r"\bdue to the fact that\b", "because"),
    (r"\bowing to the fact that\b", "because"),
    (r"\bin spite of the fact that\b", "although"),
    (r"\bat this point in time\b", "now"),
    (r"\bat the present time\b", "now"),
    (r"\bin the event that\b", "if"),
    (r"\bfor the purpose of\b", "for"),
    (r"\bwith regard to\b", "about"),
    (r"\bwith respect to\b", "about"),
    (r"\bin relation to\b", "about"),
    (r"\ba large number of\b", "many"),
    (r"\ba small number of\b", "few"),
    (r"\bthe majority of\b", "most"),
    (r"\b(is|are) able to\b", "can"),
    (r"\bhas the ability to\b", "can"),
    (r"\bmake sure (that )?", "ensure "),
    (r"\bin addition( to that)?,?", "also"),
    (r"\bas well as\b", "and"),
    (r"\bprior to\b", "before"),
    (r"\bsubsequent to\b", "after"),
    (r"\bin the near future\b", "soon"),
    (r"\bon a daily basis\b", "daily"),
    (r"\bfor example\b", "e.g."),
    (r"\bthat is to say\b", "i.e."),
    (r"\bin other words\b", "i.e."),
    (r"\bis going to\b", "will"),
    (r"\bare going to\b", "will"),
]

# Removed outright at medium and above.
FILLER = [
    r"^(hi|hello|hey)( there| claude| chatgpt| gpt)?[!,.]*\s*",
    r"\b(thanks|thank you)( so much| very much)?( in advance)?[!.]*",
    r"\bi hope (this|that) (helps|makes sense)[.!]*",
    r"\bi hope you('re| are) (doing )?(well|good|great)[.!]*",
    r"\b(please|kindly)\b,?",
    r"\bi would like you to\b", r"\bi want you to\b", r"\bi need you to\b",
    r"\b(could|can|would) you( please)?\b",
    r"\bif (it's|it is) possible,?\b", r"\bif possible,?\b",
    r"\bit is important to note that\b", r"\bit should be noted that\b",
    r"\bplease note that\b", r"\bnote that\b",
    r"\b(just|basically|actually|really|very|simply|quite|literally|totally)\b",
    r"\bas a matter of fact,?\b", r"\bto be honest,?\b",
]

# Removed at high and above.
HEDGES = [
    r"\bi think( that)?\b", r"\bi believe( that)?\b", r"\bi feel like\b",
    r"\bit seems (like|that)\b", r"\bsort of\b", r"\bkind of\b",
    r"\bin my opinion,?\b", r"\bfeel free to\b",
]
ARTICLES = r"\b(a|an|the) (?=\w)"
EMPHASIS = r"(\*\*|__)(.+?)\1"

COMMENT_LINE = re.compile(r"^\s*(#(?!!)|//|--\s)")


def _prose(s, level):
    i = LEVELS.index(level)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r" *\n *", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    if i >= 1:
        for pat, rep in PHRASES:
            s = re.sub(pat, rep, s, flags=re.I)
        for pat in FILLER:
            s = re.sub(pat, "", s, flags=re.I | re.M)
    if i >= 2:
        for pat in HEDGES:
            s = re.sub(pat, "", s, flags=re.I)
        s = re.sub(ARTICLES, "", s, flags=re.I)
        s = re.sub(EMPHASIS, r"\2", s)
    s = re.sub(r"(\*\*|__)\s*\1", "", s)
    s = re.sub(r" {2,}", " ", s)
    s = re.sub(r" +([,.;:!?])", r"\1", s)
    s = re.sub(r"([,;:])\1+|^[,;:] *", r"\1", s, flags=re.M)
    s = re.sub(r"(?<=\n) +| +(?=\n)", "", s)
    # Capitalize sentence starts that lost their first word.
    s = re.sub(r"(\n|(?<!e\.g)(?<!i\.e)[.!?] +)([a-z])", lambda m: m.group(1) + m.group(2).upper(), s)
    return s


def _dedupe(s, seen):
    """Drop sentences already seen anywhere earlier in the prose."""
    out = []
    for line in s.split("\n"):
        kept = []
        for sent in re.split(r"(?<=[.!?]) +", line):
            key = re.sub(r"\W+", " ", sent).strip().lower()
            if len(key) > 12 and key in seen:
                continue
            seen.add(key)
            kept.append(sent)
        if line.strip() and not kept:
            continue
        out.append(" ".join(kept))
    return "\n".join(out)


def _code(block, level):
    i = LEVELS.index(level)
    if not block.startswith("```") or i < 2:
        return block
    head, _, rest = block.partition("\n")
    body = rest[:-3]
    lang = head[3:].strip().lower()
    if lang in ("json", "") and body.strip()[:1] in "{[":
        try:
            return head + "\n" + json.dumps(json.loads(body), separators=(",", ":")) + "\n```"
        except ValueError:
            pass
    if i >= 3:
        lines = [l.rstrip() for l in body.split("\n")]
        lines = [l for l in lines if l.strip() and not COMMENT_LINE.match(l)]
        body = "\n".join(lines) + "\n"
    return head + "\n" + body + "```"


def compress(text, level="medium"):
    if level not in LEVELS:
        raise ValueError("level must be one of %s" % LEVELS)
    seen, out = set(), []
    for p in CODE.split(text):
        if CODE.fullmatch(p):
            out.append(_code(p, level))
        else:
            p = _prose(p, level)
            out.append(_dedupe(p, seen) if LEVELS.index(level) >= 1 else p)
    out = re.sub(r"\n{3,}", "\n\n", "".join(out)).strip()
    return out[:1].upper() + out[1:]


def count(text, model="gpt-4o"):
    """Return (tokens, exact). Uses tiktoken when installed, else ~4 chars/token."""
    try:
        import tiktoken
        try:
            enc = tiktoken.encoding_for_model(model)
        except KeyError:
            enc = tiktoken.get_encoding("o200k_base")
        return len(enc.encode(text)), True
    except ImportError:
        return max(1, round(len(text) / 4)), False


# ---------------------------------------------------------------------------
# Tool-output compression: what coding agents actually spend tokens on.
# Deterministic, so the same tool result always compresses to the same bytes
# and provider prompt caches keep hitting.
# ---------------------------------------------------------------------------

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
LINE_NO = re.compile(r"^ +(\d+)(\t|→)", re.M)
MIN_TOOL_CHARS = 200


RESERVED = ("_cols", "_rows", "_same")


def _tabulate(obj):
    """Turn lists of same-shaped objects into a table, losslessly.

    [{"id":1,"wh":"ams"}, {"id":2,"wh":"ams"}, ...] becomes
    {"_cols":["id"], "_rows":[[1],[2],...], "_same":{"wh":"ams"}}.
    `expand()` reverses it exactly.
    """
    if isinstance(obj, dict):
        return {k: _tabulate(v) for k, v in obj.items()}
    if isinstance(obj, list):
        items = [_tabulate(v) for v in obj]
        if len(items) >= 3 and all(isinstance(v, dict) for v in items):
            keys = list(items[0].keys())
            if all(len(v) == len(keys) and all(k in v for k in keys) for v in items):
                # Compare as JSON text: in Python 0 == False and 1 == 1.0, which would lose types.
                same = {}
                for k in keys:
                    first = json.dumps(items[0][k])
                    if all(json.dumps(v[k]) == first for v in items[1:]):
                        same[k] = items[0][k]
                cols = [k for k in keys if k not in same]
                table = {"_cols": cols, "_rows": [[v[c] for c in cols] for v in items]}
                if same:
                    table["_same"] = same
                return table
        return items
    return obj


def expand(obj):
    """Inverse of _tabulate: rebuild the original JSON value."""
    if isinstance(obj, dict):
        if "_cols" in obj and "_rows" in obj:
            same = obj.get("_same", {})
            # Rows may themselves be tables (a list of tables), so expand each rebuilt row.
            return [expand(dict(zip(obj["_cols"], row), **same)) for row in obj["_rows"]]
        return {k: expand(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [expand(v) for v in obj]
    return obj


def _collapse_repeats(lines):
    out, prev, n = [], None, 0
    for line in lines + [None]:
        if line == prev:
            n += 1
            continue
        if prev is not None:
            out.append(prev if n == 1 else "%s  [x%d]" % (prev, n))
        prev, n = line, 1
    return out


_VARYING = re.compile(r"\b[0-9a-f]{8,}\b|\d+", re.I)


def _collapse_similar(lines, min_run=5):
    """Lossy: runs of lines that differ only in numbers or ids keep first and last."""
    out, i = [], 0
    while i < len(lines):
        key = _VARYING.sub("#", lines[i])
        j = i + 1
        while j < len(lines) and lines[j].strip() and _VARYING.sub("#", lines[j]) == key:
            j += 1
        if j - i >= min_run:
            out += [lines[i], "  ... [%d similar lines omitted]" % (j - i - 2), lines[j - 1]]
        else:
            out += lines[i:j]
        i = j
    return out


def compress_tool(text, lossy=False):
    """Compress one tool result (JSON, logs, file reads, command output).

    Lossless by default. lossy=True also shortens runs of near-identical log lines.
    """
    if not isinstance(text, str) or len(text) < MIN_TOOL_CHARS:
        return text
    s = ANSI.sub("", text)
    t = s.strip()
    if t[:1] in "{[":
        try:
            data = json.loads(t)
            if not any('"%s"' % r in t for r in RESERVED):
                data = _tabulate(data)
            out = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
            return out if len(out) < len(text) else text
        except ValueError:
            pass
    if lossy:
        s = _compact_shapes(s)
    s = LINE_NO.sub(r"\1\2", s)
    lines = _collapse_repeats([l.rstrip() for l in s.split("\n")])
    if lossy:
        lines = _collapse_similar(lines)
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
    return out if len(out) < len(text) else text


def _tool_text(content, fn):
    """Apply fn to a tool result whose content is a string or a list of text parts."""
    if isinstance(content, str):
        return fn(content)
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") in ("text", "input_text", "output_text"):
                part["text"] = fn(part.get("text", ""))
    return content


def _assistant_texts(m):
    """Yield (label, text) for what the agent itself wrote in one message or item."""
    calls = []
    content = m.get("content")
    if isinstance(content, str):
        yield None, content
    elif isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") in ("text", "output_text"):
                yield None, b.get("text") or ""
            elif b.get("type") == "tool_use":
                calls.append((b.get("id"), b.get("input")))
    for tc in m.get("tool_calls") or []:  # OpenAI chat completions
        try:
            calls.append((tc.get("id"), json.loads(tc["function"].get("arguments") or "{}")))
        except (KeyError, TypeError, ValueError):
            pass
    if m.get("type") == "function_call":  # OpenAI Responses API
        try:
            calls.append((m.get("call_id"), json.loads(m.get("arguments") or "{}")))
        except (TypeError, ValueError):
            pass
    for call_id, args in calls:
        for v in (args.values() if isinstance(args, dict) else [args]):
            if isinstance(v, str) and "\n" in v:
                yield "your input to tool call %s" % call_id, v


def _walk(body):
    """Yield ("result", holder, key, call_id) and ("wrote", label, text) in conversation order."""
    n = 0
    seq = [m for m in body.get("messages") or [] if isinstance(m, dict)]
    seq += [it for it in body.get("input") or [] if isinstance(it, dict)] if isinstance(body.get("input"), list) else []
    for m in seq:
        if m.get("role") == "assistant" or m.get("type") == "function_call":
            n += 1
            for label, text in _assistant_texts(m):
                yield "wrote", label or "your message #%d" % n, text
        elif m.get("role") == "tool":
            yield "result", m, "content", m.get("tool_call_id")
        elif m.get("type") in ("function_call_output", "custom_tool_call_output"):
            yield "result", m, "output", m.get("call_id")
        elif isinstance(m.get("content"), list):
            for block in m["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    yield "result", block, "content", block.get("tool_use_id")


REF_MIN_RUN = 3          # shortest run of repeated lines worth a reference
REF_MIN_CHARS = 220      # ...and only when the run is ~2.5x longer than the reference
BIG_LINES = 400          # outputs longer than this become expandable in smart mode
# headforward was called leantokens; its data folder moves on first run (see _migrate_home).
HOME_DIR = os.path.join(os.path.expanduser("~"), ".headforward")
OLD_HOME = os.path.join(os.path.expanduser("~"), ".leantokens")
BLOB_DIR = os.path.join(HOME_DIR, "outputs")
_ERRORISH = re.compile(r"error|exception|traceback|fail|warn|fatal|panic|assert", re.I)


# Line-number prefixes used by agents' file viewers: "12:code", "  12\tcode", "12→code", "12|code".
_NUM_PREFIX = re.compile(r"^\s*(\d+)\s*[:\t\u2192|]\s?")
MAX_SOURCES = 16  # remembered positions per line text; bounds memory and work
_REF_LINE = re.compile(r"^\[\d+ lines[ :]")


def _key(line):
    """What a line says, ignoring its line number and trailing spaces."""
    return _NUM_PREFIX.sub("", line, count=1).rstrip()


class Shadow:
    """The model's view of a conversation: every tool-output line it has been sent.

    Lines are stored by position (call_id, line_no) and indexed by content with
    line numbers removed, so a file re-shown after an edit still matches even
    though every line below the edit was renumbered.
    """

    def __init__(self):
        self.sent = {}    # (call_id, line_no) -> key
        self.raw = {}     # (call_id, line_no) -> line exactly as sent
        self.where = {}   # key -> [(call_id, line_no), ...]

    def add(self, call_id, lines):
        for n, line in enumerate(lines):
            if _REF_LINE.match(line):
                continue  # never index our own references
            k = _key(line)
            self.sent[(call_id, n)] = k
            self.raw[(call_id, n)] = line
            if len(k.strip()) > 3:
                spots = self.where.setdefault(k, [])
                if len(spots) < MAX_SOURCES:
                    spots.append((call_id, n))

    def longest_match(self, lines, i, call_id):
        """Longest run starting at lines[i] that the model already saw contiguously."""
        best = (0, None, None)
        for c, p in self.where.get(_key(lines[i]), ()):
            if c == call_id:
                continue
            j = i + 1
            while j < len(lines) and self.sent.get((c, p + j - i)) == _key(lines[j]):
                j += 1
            if j - i > best[0]:
                best = (j - i, c, p)
        return best


def _reference_runs(out, label, shadow):
    """Replace runs of lines the model already saw with a pointer to them.

    Matching ignores line-number prefixes. When the numbers changed (lines moved
    after an edit), the reference says what they are numbered now, so the model
    can still cite exact line numbers.
    """
    lines = out.split("\n")
    res, i = [], 0
    while i < len(lines):
        k = _key(lines[i])
        n, src, pos = shadow.longest_match(lines, i, label) if len(k.strip()) > 3 else (0, None, None)
        # A reference is about 90 characters; only use one when it is clearly shorter.
        if n >= REF_MIN_RUN and len("\n".join(lines[i:i + n])) >= REF_MIN_CHARS:
            same = all(shadow.raw.get((src, pos + d)) == lines[i + d].rstrip() or
                       shadow.raw.get((src, pos + d)) == lines[i + d] for d in range(n))
            first, last = _NUM_PREFIX.match(lines[i]), _NUM_PREFIX.match(lines[i + n - 1])
            note = "[%d lines identical to lines %d-%d of %s" % (n, pos + 1, pos + n, src)
            if not same and first and last:
                note = "[%d lines: same text as lines %d-%d of %s, numbered %s-%s here" % (
                    n, pos + 1, pos + n, src, first.group(1), last.group(1))
            res.append(note + "]")
            i += n
        else:
            res.append(lines[i])
            i += 1
    return "\n".join(res)


def _rel(path):
    """Path relative to the current folder, or absolute when that is impossible (another Windows drive)."""
    try:
        return os.path.relpath(path)
    except ValueError:
        return os.path.abspath(path)


def _save_blob(text):
    """Keep the full text of an output we shortened; return its ID, or None on failure."""
    blob = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:12]
    try:
        os.makedirs(BLOB_DIR, exist_ok=True)
        path = os.path.join(BLOB_DIR, blob + ".txt")
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
    except OSError:
        return None
    return blob


# Line classes that carry no information once you know they happened.
# A run of 3+ lines of one class becomes a one-line count, unless a line looks like an error.
NOISE = [
    ("package install", re.compile(r"^\s*(Requirement already satisfied|Collecting |Downloading |Using cached |"
                                   r"Obtaining |Preparing metadata|Building wheel|Created wheel|Stored in directory|"
                                   r"Looking in indexes|Attempting uninstall|Found existing installation|"
                                   r"Uninstalling |Successfully uninstalled|npm (WARN|notice|http)|"
                                   r"(added|removed|changed|audited) \d+ packages)")),
    ("progress", re.compile(r"\d{1,3}%\s*\||[\u2588\u2501#=]{10,}|\[\s*\d+%\]\s|"
                            r"\d+(\.\d+)?\s*[kMG]i?B/s|^\s*\d+(\.\d+)?%\s*$")),
    ("passing test", re.compile(r"(PASSED|\bpass(ed)?\b\s*(\(\d+(\.\d+)?\s*m?s\))?$|\.\.\. ok$|^ok \d+ |"
                                r"^\s*[\u2713\u2714]|^test [\w:]+ \.\.\. ok$)", re.I)),
    ("build step", re.compile(r"^\s*(Compiling|Checking|Downloaded|Fresh|Updating|Locking|Adding) [\w.-]+ v?\d|"
                              r"^\s*\[\s*\d+%\] (Building|Linking|Built)|^#\d+ \[|^ ---> |^Step \d+/\d+|"
                              r"^[0-9a-f]{12}: (Pulling|Waiting|Downloading|Extracting|Verifying|Download complete|Pull complete)")),
    ("git progress", re.compile(r"^(remote: )?(Counting|Compressing|Receiving|Resolving|Enumerating|Writing) objects")),
]


def _filter_noise(text):
    """Collapse runs of routine lines (installs, progress bars, passing tests, build steps).

    Error-like lines are always kept. The full output is saved, so nothing is lost.
    """
    lines = text.split("\n")

    def cls(line):
        if _ERRORISH.search(line) and not re.search(r"\b0 (errors?|failed|failures?)\b", line, re.I):
            return None
        for name, pat in NOISE:
            if pat.search(line):
                return name
        return None

    kinds = [cls(l) for l in lines]
    if sum(1 for k in kinds if k) < 3:
        return text
    blob = None
    out, i = [], 0
    while i < len(lines):
        j = i + 1
        while j < len(lines) and kinds[i] and kinds[j] == kinds[i]:
            j += 1
        if kinds[i] and j - i >= 3:
            blob = blob or _save_blob(text)
            if not blob:
                return text
            out.append("[%d %s lines omitted; full output: headforward show %s]" % (j - i, kinds[i], blob))
        else:
            out.extend(lines[i:j])
        i = j
    return "\n".join(out)


# Shape-aware compaction for outputs agents read a lot: git log, `ls -l`,
# grep matches and timestamped logs. Each keeps every distinct fact and drops
# only what repeats or can be derived; the full output is saved for `show`.

_GIT_COMMIT = re.compile(r"^commit ([0-9a-f]{40})\b(.*)$")
_GIT_STAT = re.compile(r"^ (\S.*?)\s+\|\s+(\d+|Bin [^|]*?)\s*[+-]*$")
_LS_LONG = re.compile(r"^([dlcbps-][rwxsStT-]{9})[@+.]?\s+\d+\s+(\S+)\s+(\S+)\s+"
                      r"(\d+(?:,\s*\d+)?)\s+(\w{3}\s+\d{1,2}\s+[\d:]{4,5})\s+(.*)$")
_GREP_FILE = re.compile(r"^([^\s:][^:]*):(\d+)([:-])(.*)$")
_GREP_NUM = re.compile(r"^(\d+)([:-])(.*)$")
_LOG_TS = re.compile(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(?:\.\d+)?(?:[+-]\d{2}:?\d{2}|Z)?\s+")
_LOG_VARYING = re.compile(r"\b0x[0-9a-f]+\b|\b(?=[0-9a-f]*\d)[0-9a-f]{4,}\b|\d+", re.I)


def _compact_git_log(lines):
    if sum(1 for l in lines if _GIT_COMMIT.match(l)) < 3:
        return None
    authors = {l[7:].strip() for l in lines if l.startswith("Author:")}
    one_author = len(authors) == 1
    out = ["author of every commit: %s" % authors.pop()] if one_author else []
    for l in lines:
        m = _GIT_COMMIT.match(l)
        if m:
            out.append("commit %s%s" % (m.group(1)[:10], m.group(2)))
        elif l.startswith("Author:"):
            if not one_author:
                out.append("  by " + l[7:].strip())
        elif l.startswith("Date:"):
            raw = l[5:].strip()
            try:
                raw = datetime.datetime.strptime(raw, "%a %b %d %H:%M:%S %Y %z").strftime("%Y-%m-%d %H:%M")
            except ValueError:
                pass
            out[-1] += "  " + raw
        elif _GIT_STAT.match(l):
            m = _GIT_STAT.match(l)
            out.append("  %s | %s" % (m.group(1), m.group(2)))
        elif l.startswith("    "):
            out.append("  " + l[4:])
        elif l.strip():
            out.append(l)
    return out


def _compact_ls(lines):
    rows = [(l, _LS_LONG.match(l)) for l in lines]
    found = [m for _, m in rows if m]
    if len(found) < 8:
        return None
    owners = {(m.group(2), m.group(3)) for m in found}
    one_owner = len(owners) == 1
    out = ["owner of every entry: %s:%s" % owners.pop()] if one_owner else []
    for l, m in rows:
        if not m:
            if not re.match(r"^total \d+$", l):
                out.append(l)
            continue
        perms, owner, group, size, date, name = m.groups()
        who = "" if one_owner else " %s:%s" % (owner, group)
        out.append("%s%s %s %s %s%s" % (perms, who, size, re.sub(r"\s+", " ", date), name,
                                        "/" if perms[0] == "d" and not name.endswith("/") else ""))
    return out


def _compact_grep(lines):
    body = [l for l in lines if l]
    if len(body) < 10:
        return None
    with_file = sum(1 for l in body if _GREP_FILE.match(l))
    with_num = sum(1 for l in body if _GREP_NUM.match(l))
    if max(with_file, with_num) < 0.9 * len(body):
        return None
    out, cur = [], None
    for l in lines:
        m = _GREP_FILE.match(l) if with_file >= with_num else None
        if m:
            path, n, sep, text = m.groups()
            if path != cur:
                out.append(path)
                cur = path
            out.append("  %s%s%s" % (n, sep, text.strip()))
            continue
        m = _GREP_NUM.match(l)
        out.append("%s%s%s" % (m.group(1), m.group(2), m.group(3).strip()) if m else l)
    return out


def _compact_log(lines):
    stamps = [_LOG_TS.match(l) for l in lines]
    if len(lines) < 30 or sum(1 for m in stamps if m) < 0.5 * len(lines):
        return None
    dates = {m.group(1) for m in stamps if m}
    one_day = len(dates) == 1
    out = ["every line is from %s" % next(iter(dates))] if one_day else []
    first, count, last = {}, {}, {}
    for l, m in zip(lines, stamps):
        body = l[m.end():] if m else l
        key = _LOG_VARYING.sub("#", body)
        if m and key in first:
            count[key] += 1
            last[key] = m.group(2)
            continue
        if m:
            first[key], count[key], last[key] = len(out), 1, m.group(2)
            out.append("%s %s" % (m.group(2) if one_day else m.group(1) + " " + m.group(2), body))
        else:
            out.append(l)
    for key, i in first.items():
        if count[key] > 1:
            out[i] += "  [x%d similar, last at %s]" % (count[key], last[key])
    return out


def _compact_shapes(text):
    """Compact git log, `ls -l`, grep and timestamped log output; the full text stays readable via show."""
    lines = text.split("\n")
    # Logs before grep: "2026-10-20 17:17:00" would otherwise read as file:line:.
    for compact in (_compact_git_log, _compact_ls, _compact_log, _compact_grep):
        out = compact(lines)
        if out is None:
            continue
        short = "\n".join(out)
        if len(short) > 0.9 * len(text):
            return text
        blob = _save_blob(text)
        return short + "\n[compacted; full output: headforward show %s]" % blob if blob else text
    return text


def _expandable(text):
    """Shorten a very long output to its start, end and error lines.

    The full text is saved under ~/.headforward/outputs so the agent can read any
    part of it with `headforward show ID --lines A-B`.
    """
    lines = text.split("\n")
    if len(lines) <= BIG_LINES:
        return text
    blob = _save_blob(text)
    if not blob:
        return text
    head, tail = 80, 40
    middle = [(n, l) for n, l in enumerate(lines[head:-tail], head) if _ERRORISH.search(l)][:40]
    out = lines[:head]
    out.append("[headforward: %d of %d lines omitted. Error-like lines from the omitted part follow. "
               "To read any part, run: headforward show %s --lines A-B]" % (len(lines) - head - tail, len(lines), blob))
    out += ["%d: %s" % (n + 1, l) for n, l in middle]
    out.append("[end of excerpt; last %d lines follow]" % tail)
    return "\n".join(out + lines[-tail:])


def _is_json(text):
    if text.lstrip()[:1] not in ("{", "["):
        return False
    try:
        json.loads(text)
        return True
    except ValueError:
        return False


def compress_request(body, lossy=False, smart=False):
    """Compress tool results inside an Anthropic or OpenAI request body, in place.

    Default (lossless):
      1. Each tool result is compressed (see compress_tool).
      2. A result identical to an earlier one is replaced by a pointer to it.
    smart=True adds:
      3. Runs of 6+ lines already shown in an earlier result become a reference
         to those exact lines (agents re-open the same files constantly).
      4. Outputs over 400 lines keep start, end and error lines; the rest is
         saved locally and readable with `headforward show`.
    lossy=True also shortens runs of near-identical log lines.

    Earlier turns never change and the output is deterministic, so prompt caches
    keep hitting. Returns (chars_before, chars_after). User text is untouched.
    """
    stats = [0, 0]
    seen = {}
    shadow = Shadow()

    for event in _walk(body):
        if event[0] == "wrote":
            if smart:
                shadow.add(event[1], event[2].split("\n"))
            continue
        _, holder, key, call_id = event

        def fn(t, call_id=call_id):
            out = compress_tool(t, lossy)
            if len(t) >= MIN_TOOL_CHARS:
                h = hashlib.sha1(out.encode("utf-8", "replace")).hexdigest()
                pointer = "[Same output as tool call %s above.]" % seen.get(h)
                if h in seen and seen[h] != call_id and len(pointer) < len(out):
                    out = pointer
                else:
                    seen.setdefault(h, call_id)
                    if smart and not _is_json(out):
                        label = "tool call %s" % call_id
                        smarter = _reference_runs(_expandable(_filter_noise(out)), label, shadow)
                        # Guard: keep the smart version only if it really costs fewer tokens.
                        if count(smarter)[0] < count(out)[0]:
                            out = smarter
                        # Record exactly what the model is sent, so later references stay correct.
                        shadow.add(label, out.split("\n"))
            stats[0] += len(t)
            stats[1] += len(out)
            return out
        holder[key] = _tool_text(holder.get(key), fn)
    return stats[0], stats[1]


def add_cache_control(body):
    """Anthropic only: mark the system prompt and the conversation so far as cacheable.

    Skipped when the request already sets cache_control anywhere. Cache writes cost
    25% more than normal input and reads cost 10%, so this pays off when the same
    conversation is sent again (agents, chat apps), not for one-off requests.
    Returns True if markers were added.
    """
    if '"cache_control"' in json.dumps(body) or not body.get("messages"):
        return False
    mark = {"type": "ephemeral"}
    sys_ = body.get("system")
    if isinstance(sys_, str) and sys_:
        body["system"] = [{"type": "text", "text": sys_, "cache_control": mark}]
    elif isinstance(sys_, list) and sys_ and isinstance(sys_[-1], dict):
        sys_[-1]["cache_control"] = mark
    last = body["messages"][-1]
    if isinstance(last.get("content"), str) and last["content"]:
        last["content"] = [{"type": "text", "text": last["content"]}]
    if isinstance(last.get("content"), list) and last["content"] and isinstance(last["content"][-1], dict):
        last["content"][-1]["cache_control"] = dict(mark)
    return True


# ---------------------------------------------------------------------------
# Measuring real usage. The provider reports what it billed in every response;
# the proxy reads it so stats are real numbers, not estimates.
# ---------------------------------------------------------------------------

def _find_usage(obj, acc):
    if isinstance(obj, dict):
        u = obj.get("usage")
        if isinstance(u, dict):
            for k, v in u.items():
                if isinstance(v, (int, float)):
                    acc[k] = max(acc.get(k, 0), v)
                elif isinstance(v, dict):
                    for k2, v2 in v.items():
                        if isinstance(v2, (int, float)):
                            acc[k + "." + k2] = max(acc.get(k + "." + k2, 0), v2)
        for v in obj.values():
            if isinstance(v, (dict, list)):
                _find_usage(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            _find_usage(v, acc)


def parse_usage(raw, provider):
    """Return {"in": total input, "cached": cache reads, "out": output} or None.

    Works on plain JSON responses and on server-sent event streams.
    """
    acc = {}
    try:
        _find_usage(json.loads(raw), acc)
    except ValueError:
        for line in raw.split(b"\n"):
            if line.startswith(b"data:"):
                try:
                    _find_usage(json.loads(line[5:]), acc)
                except ValueError:
                    pass
    if not acc:
        return None
    if provider == "anthropic":
        cached = acc.get("cache_read_input_tokens", 0)
        total = acc.get("input_tokens", 0) + cached + acc.get("cache_creation_input_tokens", 0)
    else:
        cached = acc.get("input_tokens_details.cached_tokens", acc.get("prompt_tokens_details.cached_tokens", 0))
        total = acc.get("input_tokens", acc.get("prompt_tokens", 0))
    out = acc.get("output_tokens", acc.get("completion_tokens", 0))
    return {"in": int(total), "cached": int(cached), "out": int(out)}


# ---------------------------------------------------------------------------
# Local proxy. Point an agent's base URL at it; it compresses tool results and
# forwards everything else untouched, streaming included.
# ---------------------------------------------------------------------------

# Respect a base URL the user already set (a company gateway, for example).
UPSTREAMS = {
    "anthropic": os.environ.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com",
    "openai": re.sub(r"/v1/?$", "", os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com"),
}
STATS_FILE = os.path.join(HOME_DIR, "stats.jsonl")
HOP = {"host", "content-length", "accept-encoding", "connection", "keep-alive",
       "transfer-encoding", "proxy-connection", "upgrade", "te"}
SECRET_HEADERS = {"authorization", "x-api-key", "api-key", "cookie", "openai-organization", "openai-project"}
MAX_CAPTURE = 8 * 1024 * 1024


def _log_error(msg):
    try:
        os.makedirs(os.path.dirname(STATS_FILE), exist_ok=True)
        with open(os.path.join(os.path.dirname(STATS_FILE), "errors.log"), "a") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass


def _append_stats(row):
    try:
        os.makedirs(os.path.dirname(STATS_FILE), exist_ok=True)
        with open(STATS_FILE, "a") as f:
            f.write(json.dumps(row) + "\n")
    except OSError:
        pass


def _save_recording(folder, provider, path, headers, body):
    """Save an original (uncompressed) request for `headforward eval`. No secrets."""
    try:
        os.makedirs(folder, exist_ok=True)
        name = "%d-%s.json" % (time.time() * 1000, hashlib.sha1(body).hexdigest()[:8])
        keep = {k: v for k, v in headers.items() if k.lower() not in SECRET_HEADERS | HOP}
        with open(os.path.join(folder, name), "w") as f:
            json.dump({"provider": provider, "path": path, "headers": keep,
                       "body": json.loads(body)}, f)
    except (OSError, ValueError):
        pass


# ---------------------------------------------------------------------------
# HTTPS proxy mode: for clients that ignore base URLs (Claude Code in the
# desktop app), act as their HTTPS_PROXY. CONNECTs to an API host listed in
# INTERCEPT are decrypted with a local CA so the request can be compressed;
# every other host gets a blind tunnel and is never read.
# ---------------------------------------------------------------------------

INTERCEPT = {"api.anthropic.com": "https://api.anthropic.com"}
CA_DIR = os.path.join(HOME_DIR, "ca")


def _ca_paths():
    return {k: os.path.join(CA_DIR, f) for k, f in (
        ("cert", "ca.pem"), ("key", "ca-key.pem"), ("leaf", "leaf.pem"), ("leaf_key", "leaf-key.pem"),
        ("hosts", "hosts.txt"), ("cnf", "openssl.cnf"))}


def ensure_ca():
    """Create the local CA if needed and a fresh leaf certificate for the INTERCEPT hosts.

    The CA carries a name constraint, so even if its key leaked it could only
    vouch for those hosts. Keys are readable by you alone.
    """
    if not shutil.which("openssl"):
        raise RuntimeError("HTTPS mode needs the `openssl` command to make its local certificate")
    p = _ca_paths()
    hosts = sorted(INTERCEPT)
    os.makedirs(CA_DIR, mode=0o700, exist_ok=True)
    os.chmod(CA_DIR, 0o700)
    with open(p["cnf"], "w") as f:
        f.write("[req]\ndistinguished_name = dn\nprompt = no\n[dn]\nCN = headforward local CA\n"
                "[v3_ca]\nbasicConstraints = critical,CA:TRUE,pathlen:0\nkeyUsage = critical,keyCertSign,cRLSign\n"
                "subjectKeyIdentifier = hash\nnameConstraints = critical,%s\n"
                "[v3_leaf]\nbasicConstraints = critical,CA:FALSE\nkeyUsage = critical,digitalSignature,keyEncipherment\n"
                "extendedKeyUsage = serverAuth\nsubjectAltName = %s\nsubjectKeyIdentifier = hash\n"
                "authorityKeyIdentifier = keyid\n"
                % (",".join("permitted;DNS:" + h for h in hosts), ",".join("DNS:" + h for h in hosts)))

    def run(*args):
        r = subprocess.run(["openssl"] + list(args), capture_output=True, text=True, timeout=60)
        if r.returncode:
            raise RuntimeError("openssl %s failed: %s" % (args[0], r.stderr.strip()[-300:]))

    try:
        same_hosts = open(p["hosts"]).read().split() == hosts
    except OSError:
        same_hosts = False
    old = os.umask(0o077)
    try:
        if not (same_hosts and os.path.exists(p["cert"]) and os.path.exists(p["key"])):
            run("req", "-x509", "-new", "-nodes", "-newkey", "rsa:2048", "-sha256", "-days", "3650",
                "-keyout", p["key"], "-out", p["cert"], "-config", p["cnf"], "-extensions", "v3_ca")
            with open(p["hosts"], "w") as f:
                f.write("\n".join(hosts) + "\n")
        # A new leaf every start: cheap, and it never gets near expiry (397 days, the browser limit).
        csr = os.path.join(CA_DIR, "leaf.csr")
        run("req", "-new", "-nodes", "-newkey", "rsa:2048", "-keyout", p["leaf_key"], "-out", csr,
            "-subj", "/CN=" + hosts[0], "-config", p["cnf"])
        run("x509", "-req", "-in", csr, "-CA", p["cert"], "-CAkey", p["key"], "-sha256", "-days", "397",
            "-set_serial", str(int.from_bytes(os.urandom(8), "big")), "-out", p["leaf"],
            "-extfile", p["cnf"], "-extensions", "v3_leaf")
        os.remove(csr)
    finally:
        os.umask(old)
    return p


def _tls_context(paths):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(paths["leaf"], paths["leaf_key"])
    ctx.set_alpn_protocols(["http/1.1"])
    return ctx


def _pipe(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


# Endpoints that are model calls; only these get a stats row (not token counting or telemetry).
MODEL_CALL = re.compile(r"/(messages|chat/completions|responses|completions)$")


def make_handler(upstream=None, log=None, lossy=False, record=None, smart=False, cache=False):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _forward(self):
            # Set on requests decrypted from a CONNECT to an INTERCEPT host.
            origin = getattr(self, "origin", None)
            passthrough = False
            if self.path.startswith(("http://", "https://")):
                # Absolute form: a client using us as its HTTP proxy.
                u = urllib.parse.urlsplit(self.path)
                self.path = urllib.parse.urlunsplit(("", "", u.path or "/", u.query, ""))
                if not _is_self(u, self.server):
                    origin, passthrough = "%s://%s" % (u.scheme, u.netloc), True
            if self.path in ("/headforward/health", "/leantokens/health"):
                out = json.dumps({"ok": True, "pid": os.getpid()}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
                return
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else b""
            anthropic = (self.path.startswith("/v1/messages") or "anthropic-version" in self.headers
                         or origin in INTERCEPT.values())
            provider = "anthropic" if anthropic else "openai"
            model_call = bool(MODEL_CALL.search(urllib.parse.urlsplit(self.path).path))
            base = urllib.parse.urlsplit(origin or upstream or UPSTREAMS[provider])
            row = None

            # Only LLM API traffic is compressed; anything else proxied passes through untouched.
            is_json = not passthrough and body and "json" in (self.headers.get("Content-Type") or "")
            if is_json and record and self.command == "POST":
                _save_recording(record, provider, self.path, dict(self.headers.items()), body)
            if is_json:
                try:
                    data = json.loads(body)
                    before, after = compress_request(data, lossy, smart)
                    cached = cache and provider == "anthropic" and add_cache_control(data)
                    if before > after or cached:
                        body = json.dumps(data, ensure_ascii=False).encode()
                    # Record every model call, even with nothing to compress, so stats show all traffic.
                    if model_call:
                        row = {"t": int(time.time()), "p": provider, "b": before, "a": after,
                               "req": len(body)}
                except ValueError:
                    pass
                except Exception as e:  # never break the agent: send the request unchanged
                    _log_error("compression skipped for %s %s: %r" % (self.command, self.path, e))
                    row = None

            headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
            headers["Content-Length"] = str(len(body))
            Conn = http.client.HTTPSConnection if base.scheme == "https" else http.client.HTTPConnection
            try:
                conn = Conn(base.netloc, timeout=600)
                conn.request(self.command, base.path.rstrip("/") + self.path, body or None, headers)
                resp = conn.getresponse()
            except OSError as e:
                msg = ("headforward: could not reach %s (%s)" % (base.netloc, e)).encode()
                self.send_response(502)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(msg)))
                self.end_headers()
                self.wfile.write(msg)
                return

            self.send_response(resp.status, resp.reason)
            for k, v in resp.getheaders():
                if k.lower() not in HOP:
                    self.send_header(k, v)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            captured, size = [], 0
            try:
                while True:
                    chunk = resp.read1(65536)
                    if not chunk:
                        break
                    if row is not None and size < MAX_CAPTURE:
                        captured.append(chunk)
                        size += len(chunk)
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                conn.close()

            if row is not None:
                usage = parse_usage(b"".join(captured), provider) if resp.status == 200 else None
                if usage:
                    row.update(usage)
                _append_stats(row)
                if log:
                    log(row)

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _forward

        def do_CONNECT(self):
            host, _, port = self.path.rpartition(":")
            self.close_connection = True
            ctx = getattr(self.server, "tls_context", None)
            if ctx and host in INTERCEPT:
                self.send_response(200, "Connection Established")
                self.end_headers()
                try:
                    tls = ctx.wrap_socket(self.connection, server_side=True)
                except (ssl.SSLError, OSError) as e:
                    _log_error("TLS handshake for %s failed: %r. Does the client trust %s? "
                               "(NODE_EXTRA_CA_CERTS)" % (host, e, _ca_paths()["cert"]))
                    return
                inner = Handler.__new__(Handler)
                inner.origin = INTERCEPT[host]
                Handler.__init__(inner, tls, self.client_address, self.server)
                return
            try:
                up = socket.create_connection((host, int(port or 443)), timeout=30)
                up.settimeout(None)
            except (OSError, ValueError) as e:
                self.send_error(502, "headforward: could not reach %s (%s)" % (self.path, e))
                return
            self.send_response(200, "Connection Established")
            self.end_headers()
            threading.Thread(target=_pipe, args=(up, self.connection), daemon=True).start()
            _pipe(self.connection, up)

    return Handler


def _is_self(url, server):
    host = (url.hostname or "").lower()
    return host in ("127.0.0.1", "localhost", "::1") and (url.port or 80) == server.server_address[1]


class _QuietServer(http.server.ThreadingHTTPServer):
    """Never print tracebacks: under `wrap` they would land on top of the agent's screen."""

    def handle_error(self, request, client_address):
        import traceback
        _log_error("request from %s failed:\n%s" % (client_address, traceback.format_exc()))


def start_proxy(port=0, upstream=None, log=None, lossy=False, record=None, smart=False, cache=False,
                https=False):
    handler = make_handler(upstream, log, lossy, record, smart, cache)
    srv = _QuietServer(("127.0.0.1", port), handler)
    srv.tls_context = _tls_context(ensure_ca()) if https else None
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


AGENTS = {
    "claude": ("ANTHROPIC_BASE_URL", ""),
    "codex": ("OPENAI_BASE_URL", "/v1"),
    "aider": ("OPENAI_API_BASE", "/v1"),
}


def summarize(rows):
    """Turn stats rows into real totals plus an estimate of tokens saved.

    Tokens saved are estimated from characters removed, converted at the
    tokens-per-character rate the provider actually billed for those requests.
    """
    s = {"requests": sum(r["p"] != "hook" for r in rows), "in": 0, "cached": 0, "out": 0, "saved": 0.0, "measured": 0}
    for r in rows:
        removed = r["b"] - r["a"]
        if "in" in r and r.get("req"):
            s["measured"] += 1
            s["in"] += r["in"]
            s["cached"] += r["cached"]
            s["out"] += r["out"]
            s["saved"] += removed * r["in"] / r["req"]
        else:
            s["saved"] += removed / 4.0
    s["saved"] = int(s["saved"])
    return s


def _fmt_summary(s):
    if not s["in"]:
        return "~%s tokens saved (estimate; no usage reported yet)" % format(s["saved"], ",")
    pct = 100.0 * s["saved"] / (s["in"] + s["saved"])
    cache = 100.0 * s["cached"] / s["in"]
    return ("%s input tokens billed, ~%s saved (%.0f%% less input), cache hits %.0f%%, %s output"
            % (format(s["in"], ","), format(s["saved"], ","), pct, cache, format(s["out"], ",")))


def _flag(args, name):
    if name in args:
        args = [a for a in args if a != name]
        return True, args
    return False, args


def _option(args, name):
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            return args[i + 1], args[:i] + args[i + 2:]
    return None, args


def _claude_signed_in(env):
    """True, False, or None if unknown. Uses `claude auth status`, which is offline and free."""
    if env.get("ANTHROPIC_API_KEY") or env.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    try:
        out = subprocess.run(["claude", "auth", "status"], capture_output=True, text=True, timeout=15, env=env)
        return bool(json.loads(out.stdout).get("loggedIn"))
    except (OSError, ValueError, subprocess.SubprocessError, AttributeError):
        return None


def _run_agent(cmd, env):
    """Run the agent in the foreground. Ctrl-C belongs to the agent (it cancels a
    response in Claude Code), so headforward ignores it until the agent exits."""
    old = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        proc = subprocess.Popen(cmd, env=env)
        return proc.wait()
    finally:
        signal.signal(signal.SIGINT, old)


def _get_base_url(path):
    """ANTHROPIC_BASE_URL in one Claude Code settings file, or None."""
    try:
        with open(path) as f:
            return (json.load(f).get("env") or {}).get("ANTHROPIC_BASE_URL")
    except (OSError, ValueError, AttributeError):
        return None


def _claude_settings_base_url(cwd=None):
    """ANTHROPIC_BASE_URL from Claude Code settings files (user, then project), or None."""
    cwd = cwd or os.getcwd()
    found = None
    for path in (os.path.join(os.path.expanduser("~"), ".claude", "settings.json"),
                 os.path.join(cwd, ".claude", "settings.json"),
                 os.path.join(cwd, ".claude", "settings.local.json")):
        found = _get_base_url(path) or found
    return found


def cmd_wrap(argv, upstream=None):
    lossy, argv = _flag(argv, "--lossy")
    smart, argv = _flag(argv, "--smart")
    cache, argv = _flag(argv, "--cache")
    record, argv = _option(argv, "--record")
    if not argv:
        print(USAGE, file=sys.stderr)
        return 2
    agent, args = argv[0], argv[1:]
    if not shutil.which(agent):
        print("headforward: '%s' is not installed or not on your PATH" % agent, file=sys.stderr)
        return 127
    smart = smart or bool(_env("SMART"))
    lossy = lossy or bool(_env("LOSSY"))
    env = dict(os.environ)

    if agent == "claude" and _claude_signed_in(env) is False:
        print("headforward: Claude Code is not signed in on this computer. Type /login inside Claude Code\n"
              "            once (or set ANTHROPIC_API_KEY); headforward passes your sign-in through.",
              file=sys.stderr)

    rows = []
    try:
        srv = start_proxy(upstream=upstream, log=rows.append, lossy=lossy, record=record, smart=smart, cache=cache)
    except OSError as e:
        # Fail open: the agent still runs, just without compression.
        print("headforward: could not start the proxy (%s); running %s without compression" % (e, agent),
              file=sys.stderr)
        return _run_agent([agent] + args, env)

    url = "http://127.0.0.1:%d" % srv.server_address[1]
    var, suffix = AGENTS.get(agent, ("OPENAI_BASE_URL", "/v1"))
    env[var] = url + suffix
    if agent == "aider":
        env["ANTHROPIC_BASE_URL"] = url
    mode =" + ".join(m for m, on in (("smart", smart), ("lossy", lossy), ("cache", cache)) if on) or "lossless"
    print("headforward: compressing %s (%s). Savings print when it exits; `headforward stats` for totals."
          % (agent, mode), file=sys.stderr)
    try:
        code = _run_agent([agent] + args, env)
    finally:
        srv.shutdown()
    if rows:
        print("headforward: this session %s" % _fmt_summary(summarize(rows)), file=sys.stderr)
    else:
        print("headforward: no model requests went through the proxy this session.", file=sys.stderr)
    return code


def cmd_stats():
    rows = []
    try:
        with open(STATS_FILE) as f:
            rows = [json.loads(l) for l in f if l.strip()]
    except (OSError, ValueError):
        pass
    if not rows:
        print("No savings recorded yet. Run an agent through `headforward wrap claude` first.")
        return 0
    day = time.time() - 86400
    for label, sel in (("Last 24 hours", [r for r in rows if r["t"] >= day]), ("All time", rows)):
        s = summarize(sel)
        print("%-14s %5d requests  %s" % (label, s["requests"], _fmt_summary(s)))
    return 0


# ---------------------------------------------------------------------------
# Replay evaluation: prove savings and unchanged results on your own traffic.
# ---------------------------------------------------------------------------

def _api(provider, path, body, headers):
    base = UPSTREAMS[provider]
    h = {"Content-Type": "application/json"}
    h.update({k: v for k, v in headers.items() if k.lower() in ("anthropic-version", "anthropic-beta")})
    if provider == "anthropic":
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("set ANTHROPIC_API_KEY to run eval on Anthropic requests")
        h["x-api-key"] = key
        h.setdefault("anthropic-version", "2023-06-01")
    else:
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("set OPENAI_API_KEY to run eval on OpenAI requests")
        h["Authorization"] = "Bearer " + key
    req = urllib.request.Request(base.rstrip("/") + path, json.dumps(body).encode(), h)
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError("%s %s: %s" % (e.code, path, e.read()[:300].decode("utf-8", "replace")))


def _answer(provider, resp):
    """Reduce a response to what the agent would act on: the tool call, else the text."""
    if provider == "anthropic":
        blocks = resp.get("content") or []
    else:
        blocks = resp.get("output") or []
        if "choices" in resp:
            msg = resp["choices"][0]["message"]
            calls = msg.get("tool_calls") or []
            if calls:
                f = calls[0]["function"]
                return ("tool", f["name"], json.loads(f.get("arguments") or "{}"))
            return ("text", msg.get("content") or "")
    for b in blocks:
        if b.get("type") == "tool_use":
            return ("tool", b["name"], b.get("input"))
        if b.get("type") == "function_call":
            return ("tool", b["name"], json.loads(b.get("arguments") or "{}"))
    text = []
    for b in blocks:
        if b.get("type") == "text":
            text.append(b.get("text", ""))
        for c in b.get("content") or []:
            if isinstance(c, dict) and c.get("type") == "output_text":
                text.append(c.get("text", ""))
    return ("text", "".join(text))


def _same(a, b):
    if a[0] == "tool" or b[0] == "tool":
        return "same" if a == b else ("same tool" if a[:2] == b[:2] else "different")
    r = difflib.SequenceMatcher(None, a[1], b[1]).ratio()
    return "same" if r >= 0.9 else ("similar" if r >= 0.6 else "different")


def cmd_eval(argv):
    ap = argparse.ArgumentParser(prog="headforward eval",
                                 description="Replay recorded requests with and without compression.")
    ap.add_argument("folder", help="folder written by `wrap --record` or `proxy --record`")
    ap.add_argument("--tokens-only", action="store_true",
                    help="only count tokens (free on Anthropic); don't generate answers")
    ap.add_argument("--limit", type=int, default=50, help="max requests to replay (default 50)")
    ap.add_argument("--lossy", action="store_true", help="evaluate lossy mode")
    ap.add_argument("--smart", action="store_true", help="evaluate smart mode")
    ap.add_argument("--max-tokens", type=int, default=1024)
    a = ap.parse_args(argv)

    files = sorted(f for f in os.listdir(a.folder) if f.endswith(".json"))[-a.limit:]
    if not files:
        print("No recordings in %s" % a.folder)
        return 1
    tot = {"before": 0, "after": 0}
    verdicts = {}
    print("%-34s %9s %9s %6s  %s" % ("request", "tokens", "after", "saved", "answer"))
    for name in files:
        rec = json.load(open(os.path.join(a.folder, name)))
        p, path, orig = rec["provider"], rec["path"], rec["body"]
        comp = json.loads(json.dumps(orig))
        compress_request(comp, a.lossy, a.smart)
        try:
            if p == "anthropic":
                keys = ("model", "system", "messages", "tools", "tool_choice", "thinking")
                ct = lambda b: _api(p, "/v1/messages/count_tokens", {k: b[k] for k in keys if k in b},
                                    rec["headers"])["input_tokens"]
                tb, ta = ct(orig), ct(comp)
            else:
                tb = count(json.dumps(orig))[0]
                ta = count(json.dumps(comp))[0]
            verdict = "-"
            if not a.tokens_only:
                runs = []
                for b in (orig, comp):
                    b = dict(b, stream=False)
                    if "max_tokens" in b:
                        b["max_tokens"] = min(b["max_tokens"], a.max_tokens)
                    if "max_output_tokens" in b:
                        b["max_output_tokens"] = min(b["max_output_tokens"], a.max_tokens)
                    b.pop("stream_options", None)
                    if "thinking" not in b and "reasoning" not in b:
                        b["temperature"] = 0
                    runs.append(_answer(p, _api(p, path, b, rec["headers"])))
                verdict = _same(*runs)
                verdicts[verdict] = verdicts.get(verdict, 0) + 1
        except (RuntimeError, OSError, KeyError, ValueError) as e:
            print("%-34s error: %s" % (name[:34], e))
            continue
        tot["before"] += tb
        tot["after"] += ta
        print("%-34s %9s %9s %5.1f%%  %s" % (name[:34], format(tb, ","), format(ta, ","),
                                              100.0 * (tb - ta) / tb if tb else 0, verdict))
    b, af = tot["before"], tot["after"]
    if b:
        print("\nTotal input: %s -> %s tokens (%.1f%% saved)" % (format(b, ","), format(af, ","), 100.0 * (b - af) / b))
    if verdicts:
        n = sum(verdicts.values())
        print("Answers: " + ", ".join("%d %s" % (v, k) for k, v in sorted(verdicts.items())) + " (of %d)" % n)
        print("Note: models are not fully deterministic. Run eval with compression off on the same\n"
              "recordings to see the baseline rate of 'different' answers.")
    return 0


def cmd_show(argv):
    ap = argparse.ArgumentParser(prog="headforward show", description="Print an output that --smart shortened.")
    ap.add_argument("id", help="the ID from the [headforward: ... omitted] note")
    ap.add_argument("--lines", help="line range, e.g. 120-180")
    a = ap.parse_args(argv)
    if not re.fullmatch(r"[0-9a-f]{12}", a.id):
        print("headforward: not a valid output ID: %s" % a.id, file=sys.stderr)
        return 2
    try:
        lines = open(os.path.join(BLOB_DIR, a.id + ".txt"), encoding="utf-8").read().split("\n")
    except OSError:
        print("headforward: no saved output with ID %s" % a.id, file=sys.stderr)
        return 1
    lo, hi = 1, len(lines)
    if a.lines:
        m = re.fullmatch(r"(\d+)-(\d+)", a.lines)
        if not m:
            print("headforward: --lines must look like 120-180", file=sys.stderr)
            return 2
        lo, hi = max(1, int(m.group(1))), min(len(lines), int(m.group(2)))
    for n in range(lo, hi + 1):
        print("%d: %s" % (n, lines[n - 1]))
    return 0


# ---------------------------------------------------------------------------
# Claude helpers: setup check, clipboard, file packing, exact token counts.
# ---------------------------------------------------------------------------

CLAUDE_MODEL = "claude-sonnet-5-5"


def _ok(msg):
    print("  ✓ " + msg)


def _bad(msg, fix):
    print("  ✗ " + msg)
    print("      fix: " + fix)


def cmd_doctor(argv):
    """Check everything headforward needs to work with Claude, and say how to fix what isn't."""
    print("headforward doctor\n")
    problems = 0
    if sys.version_info >= (3, 9):
        _ok("Python %d.%d" % sys.version_info[:2])
    else:
        problems += 1
        _bad("Python %d.%d is too old" % sys.version_info[:2], "install Python 3.9 or newer")

    where = shutil.which("headforward")
    if where:
        _ok("headforward command on PATH (%s)" % where)
    else:
        problems += 1
        _bad("`headforward` is not on your PATH",
             "pip install the package, or run the file directly: python3 headforward.py ...")

    claude = shutil.which("claude")
    if claude:
        try:
            ver = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=15).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            ver = "version unknown"
        _ok("Claude Code installed (%s)" % (ver or "version unknown"))
        signed = _claude_signed_in(dict(os.environ))
        if signed:
            _ok("Claude Code is signed in" if not os.environ.get("ANTHROPIC_API_KEY") else "ANTHROPIC_API_KEY is set")
        elif signed is False:
            problems += 1
            _bad("Claude Code is not signed in in the terminal (the desktop app's sign-in is separate)",
                 "run `claude`, type /login, then exit")
        else:
            print("  ? could not check Claude Code sign-in")
    else:
        print("  - Claude Code not installed (only needed for `headforward wrap claude`)")

    base = os.environ.get("ANTHROPIC_BASE_URL")
    if base:
        host = urllib.parse.urlsplit(base).hostname or ""
        if host in ("127.0.0.1", "localhost"):
            try:
                urllib.request.urlopen(base, timeout=3)
                reachable = True
            except urllib.error.HTTPError:
                reachable = True
            except OSError:
                reachable = False
            if reachable:
                _ok("ANTHROPIC_BASE_URL points at a running local server (%s)" % base)
            else:
                problems += 1
                _bad("ANTHROPIC_BASE_URL is %s but nothing is listening there, so Claude requests will fail" % base,
                     "unset ANTHROPIC_BASE_URL, or start that proxy; `headforward wrap claude` needs neither")
        else:
            _ok("ANTHROPIC_BASE_URL is %s; headforward forwards there" % base)
    configured = _claude_settings_base_url()
    if configured and base and base.rstrip("/") != configured.rstrip("/"):
        # The environment beats the env block in Claude Code's settings.json.
        problems += 1
        _bad("your shell's ANTHROPIC_BASE_URL=%s overrides Claude Code settings (%s)" % (base, configured),
             "unset ANTHROPIC_BASE_URL in this shell if you want Claude Code to use %s" % configured)
    elif configured and configured.rstrip("/") == DESKTOP_URL:
        _ok("Claude Code uses the headforward background proxy (%s)" % DESKTOP_URL)

    srv = None
    try:
        srv = start_proxy()
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=20)
        c.request("POST", "/v1/messages", json.dumps({"model": CLAUDE_MODEL, "max_tokens": 1,
                                                      "messages": [{"role": "user", "content": "ping"}]}),
                  {"Content-Type": "application/json", "anthropic-version": "2023-06-01"})
        resp = c.getresponse()
        status = resp.status
        resp.read()
        c.close()
        if status in (401, 403):
            _ok("proxy reaches the Anthropic API (keyless test request was rejected as expected)")
        elif status == 502:
            problems += 1
            _bad("proxy could not reach %s" % UPSTREAMS["anthropic"], "check your internet connection or proxy settings")
        else:
            _ok("proxy reaches the Anthropic API (status %d)" % status)
    except OSError as e:
        problems += 1
        _bad("could not start the local proxy (%s)" % e, "check that nothing blocks local ports")
    finally:
        if srv:
            srv.shutdown()

    try:
        os.makedirs(os.path.dirname(STATS_FILE), exist_ok=True)
        with open(STATS_FILE, "a"):
            pass
        _ok("stats file writable (%s)" % STATS_FILE)
    except OSError:
        problems += 1
        _bad("cannot write %s" % STATS_FILE, "check permissions on ~/.headforward")

    try:
        import tiktoken  # noqa: F401
        _ok("tiktoken installed (exact counts for OpenAI models)")
    except ImportError:
        print("  - tiktoken not installed (optional): pip install tiktoken")
    if os.environ.get("ANTHROPIC_API_KEY"):
        _ok("exact Claude token counts available (`headforward count`)")
    else:
        print("  - set ANTHROPIC_API_KEY for exact Claude token counts (optional; counting is free)")

    print("\n" + ("All good. Run: headforward wrap claude" if not problems else
                  "%d problem%s found; fix %s and run `headforward doctor` again."
                  % (problems, "" if problems == 1 else "s", "it" if problems == 1 else "them")))
    return 1 if problems else 0


def _clipboard_cmds():
    if sys.platform == "darwin":
        return ["pbpaste"], ["pbcopy"]
    if os.name == "nt":
        return (["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
                ["powershell", "-NoProfile", "-Command", "$input | Set-Clipboard"])
    if shutil.which("wl-paste"):
        return ["wl-paste", "--no-newline"], ["wl-copy"]
    if shutil.which("xclip"):
        return ["xclip", "-o", "-selection", "clipboard"], ["xclip", "-selection", "clipboard"]
    if shutil.which("xsel"):
        return ["xsel", "--clipboard", "--output"], ["xsel", "--clipboard", "--input"]
    return None, None


def read_clipboard():
    get, _ = _clipboard_cmds()
    if not get:
        raise RuntimeError("no clipboard tool found (install xclip, xsel or wl-clipboard)")
    return subprocess.run(get, capture_output=True, timeout=10).stdout.decode("utf-8", "replace")


def write_clipboard(text):
    _, put = _clipboard_cmds()
    if not put:
        raise RuntimeError("no clipboard tool found (install xclip, xsel or wl-clipboard)")
    subprocess.run(put, input=text.encode("utf-8"), timeout=10, check=True)


def compress_any(text, level="medium"):
    """Tool-output rules for data and logs, prompt rules for prose."""
    tool = compress_tool(text)
    if tool != text:
        return tool
    return compress(text, level)


def _saving_line(before, after):
    b, exact = count(before)
    a, _ = count(after)
    pct = 100.0 * (b - a) / b if b else 0
    return "%s -> %s tokens, %.0f%% fewer%s" % (format(b, ","), format(a, ","), pct, "" if exact else " (estimate)")


def cmd_clip(argv):
    ap = argparse.ArgumentParser(prog="headforward clip",
                                 description="Compress the clipboard in place, ready to paste into claude.ai.")
    ap.add_argument("-l", "--level", default="medium", choices=LEVELS, help="prompt level for prose (default medium)")
    ap.add_argument("--print", action="store_true", help="print the result instead of replacing the clipboard")
    a = ap.parse_args(argv)
    try:
        text = read_clipboard()
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        print("headforward: could not read the clipboard: %s" % e, file=sys.stderr)
        return 1
    if not text.strip():
        print("headforward: the clipboard is empty. Copy some text first.", file=sys.stderr)
        return 1
    out = compress_any(text, a.level)
    if a.print:
        sys.stdout.write(out + "\n")
    else:
        try:
            write_clipboard(out)
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            print("headforward: could not write the clipboard: %s" % e, file=sys.stderr)
            return 1
    print("headforward: clipboard %s. %s" % ("printed" if a.print else "compressed, ready to paste",
                                             _saving_line(text, out)), file=sys.stderr)
    return 0


SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "env", "__pycache__", "dist", "build", ".next", ".nuxt",
             "target", ".tox", ".mypy_cache", ".pytest_cache", ".idea", ".vscode", "coverage", ".cache",
             ".gradle", "vendor", "Pods", ".terraform", ".vercel"}
SKIP_FILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Cargo.lock", "Gemfile.lock",
              "composer.lock", "go.sum", "Pipfile.lock", "uv.lock", ".DS_Store"}
SKIP_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz", ".tar", ".tgz", ".bz2",
            ".7z", ".woff", ".woff2", ".ttf", ".otf", ".mp3", ".mp4", ".mov", ".wav", ".so", ".dylib", ".dll",
            ".exe", ".bin", ".pyc", ".class", ".jar", ".o", ".a", ".wasm", ".sqlite", ".db", ".map"}
SECRET_NAMES = re.compile(r"(^\.env(\..*)?$|\.pem$|\.key$|^id_(rsa|ed25519|ecdsa)|credentials|secrets?\.)", re.I)


def _git_files(root):
    try:
        out = subprocess.run(["git", "-C", root, "ls-files", "-co", "--exclude-standard"],
                             capture_output=True, text=True, timeout=30)
        if out.returncode == 0:
            return [os.path.join(root, f) for f in out.stdout.splitlines() if f]
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _collect(paths):
    files, skipped = [], []
    for p in paths:
        if os.path.isfile(p):
            files.append(p)
            continue
        listed = _git_files(p)
        if listed is None:
            listed = []
            for d, dirs, names in os.walk(p):
                dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS)
                listed += [os.path.join(d, n) for n in sorted(names)]
        for f in listed:
            parts = set(os.path.normpath(f).split(os.sep))
            if parts & SKIP_DIRS:
                continue
            files.append(f)
    out = []
    for f in files:
        name = os.path.basename(f)
        if name in SKIP_FILES or os.path.splitext(name)[1].lower() in SKIP_EXT:
            skipped.append((f, "lockfile or binary"))
        elif SECRET_NAMES.search(name):
            skipped.append((f, "may contain secrets"))
        else:
            out.append(f)
    return out, skipped


def pack_files(paths, budget=None, outline_code=False):
    """Build a Claude-friendly <documents> block. Returns (text, included, skipped)."""
    files, skipped = _collect(paths)
    docs, included = [], []
    used = count("<documents>\n\n</documents>")[0] + 1  # the wrapper and joins count too
    for f in files:
        try:
            raw = open(f, "rb").read()
        except OSError as e:
            skipped.append((f, "unreadable (%s)" % e.strerror))
            continue
        if b"\0" in raw[:8192]:
            skipped.append((f, "binary"))
            continue
        text = raw.decode("utf-8", "replace")
        ext = os.path.splitext(f)[1].lower()
        sig = outline(f, text) if outline_code else None
        if sig:
            body = "(outline only: %d definitions from %d lines; ask for the full file if needed)\n%s" % (
                len(sig), text.count("\n") + 1, "\n".join(sig))
        elif ext in (".json", ".log", ".jsonl", ".ndjson") or "log" in os.path.basename(f).lower():
            body = compress_tool(text)
        else:
            body = "\n".join(l.rstrip() for l in text.split("\n")).strip("\n")
        doc = '<document path="%s">\n%s\n</document>' % (_rel(f), body)
        cost = count(doc)[0]
        if budget and used + cost > budget:
            skipped.append((f, "over the token budget"))
            continue
        used += cost
        docs.append(doc)
        included.append((f, len(text), cost))
    return "<documents>\n" + "\n".join(docs) + "\n</documents>", included, skipped


def cmd_pack(argv):
    ap = argparse.ArgumentParser(prog="headforward pack",
                                 description="Bundle files into one compact block to paste into claude.ai.")
    ap.add_argument("paths", nargs="+", help="files or folders (git-ignored files are left out)")
    ap.add_argument("--max-tokens", type=int, help="stop adding files once this many tokens are used")
    ap.add_argument("-o", "--out", help="write to this file instead of the clipboard")
    ap.add_argument("--print", action="store_true", help="print to the terminal instead of the clipboard")
    ap.add_argument("--outline", action="store_true",
                    help="send only class and function signatures for source code (much smaller)")
    a = ap.parse_args(argv)
    text, included, skipped = pack_files(a.paths, a.max_tokens, a.outline)
    if not included:
        print("headforward: no text files to pack.", file=sys.stderr)
        return 1
    raw_chars = sum(n for _, n, _ in included)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        dest = "written to %s" % a.out
    elif a.print:
        sys.stdout.write(text + "\n")
        dest = "printed"
    else:
        try:
            write_clipboard(text)
            dest = "copied to the clipboard, ready to paste"
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            print("headforward: could not write the clipboard (%s); use --out FILE" % e, file=sys.stderr)
            return 1
    tokens, exact = count(text)
    print("headforward: packed %d file%s, %s tokens%s (from %s characters of source), %s."
          % (len(included), "" if len(included) == 1 else "s", format(tokens, ","),
             "" if exact else " (estimate)", format(raw_chars, ","), dest), file=sys.stderr)
    top = sorted(included, key=lambda x: -x[2])[:5]
    if len(included) > 1:
        print("  largest: " + ", ".join("%s (%s)" % (_rel(f), format(t, ",")) for f, _, t in top),
              file=sys.stderr)
    for f, why in skipped[:15]:
        print("  skipped %s: %s" % (_rel(f), why), file=sys.stderr)
    if len(skipped) > 15:
        print("  ...and %d more skipped" % (len(skipped) - 15), file=sys.stderr)
    return 0


def claude_count(text, model=CLAUDE_MODEL):
    """Exact Claude input tokens via Anthropic's free count_tokens endpoint, or None."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    body = {"model": model, "messages": [{"role": "user", "content": text}]}
    return _api("anthropic", "/v1/messages/count_tokens", body, {})["input_tokens"]


def cmd_count(argv):
    ap = argparse.ArgumentParser(prog="headforward count", description="Count tokens for Claude, before and after compression.")
    ap.add_argument("file", nargs="?", default="-", help="file, or - for stdin (default)")
    ap.add_argument("--model", default=CLAUDE_MODEL, help="Claude model (default %s)" % CLAUDE_MODEL)
    ap.add_argument("-l", "--level", default="medium", choices=LEVELS)
    a = ap.parse_args(argv)
    text = sys.stdin.read() if a.file == "-" else open(a.file, encoding="utf-8").read()
    out = compress_any(text, a.level)
    try:
        before, after = claude_count(text, a.model), None
        if before is not None:
            after = claude_count(out, a.model)
    except RuntimeError as e:
        print("headforward: Anthropic count failed (%s); showing an estimate" % e, file=sys.stderr)
        before = None
    if before is None:
        before, after = max(1, len(text) // 4), max(1, len(out) // 4)
        how = "estimate; set ANTHROPIC_API_KEY for exact Claude counts (free)"
    else:
        how = "exact, %s" % a.model
    pct = 100.0 * (before - after) / before if before else 0
    print("as is:            %s tokens" % format(before, ","))
    print("after headforward: %s tokens  (%.0f%% fewer)" % (format(after, ","), pct))
    print("(%s)" % how)
    return 0


# ---------------------------------------------------------------------------
# audit / tune: find and fix the habits that waste Claude Code tokens.
# ---------------------------------------------------------------------------

MEMORY_WARN = 2000        # CLAUDE.md is sent with every request; flag it above this
BIG_FILE_TOKENS = 25000   # a single file this big costs a lot every time an agent reads it
OUTPUT_CAP = 15000  # bashOutputMaxChars: inline characters per command (Claude Code default 30000; rest saved to a file)
BUILD_DIRS = ["dist", "build", "out", ".next", ".nuxt", "coverage", "target", "vendor", ".turbo", "storybook-static"]
LOCKFILES = sorted(SKIP_FILES - {".DS_Store"})
GENERATED = ["*.min.js", "*.min.css", "*.map"]


def _tok(text):
    return count(text)[0]


def _memory_files(root):
    home = os.path.expanduser("~")
    found = []
    for p in [os.path.join(home, ".claude", "CLAUDE.md"), os.path.join(root, "CLAUDE.md"),
              os.path.join(root, ".claude", "CLAUDE.md"), os.path.join(root, "CLAUDE.local.md")]:
        if os.path.isfile(p):
            found.append(p)
    return found


def _settings(root):
    merged = {}
    for name in ("settings.json", "settings.local.json"):
        try:
            with open(os.path.join(root, ".claude", name)) as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        merged.setdefault("deny", []).extend(data.get("permissions", {}).get("deny", []))
        merged.setdefault("env", {}).update(data.get("env", {}))
        if "bashOutputMaxChars" in data:
            merged["bashOutputMaxChars"] = data["bashOutputMaxChars"]
    return merged


def _big_files(root, limit):
    files, _ = _collect([root])
    out = []
    for f in files:
        try:
            size = os.path.getsize(f)
        except OSError:
            continue
        if size < limit * 3:  # cheap pre-filter: under ~3 chars/token can't be this big
            continue
        try:
            raw = open(f, "rb").read()
        except OSError:
            continue
        if b"\0" in raw[:8192]:
            continue
        t = _tok(raw.decode("utf-8", "replace"))
        if t >= limit:
            out.append((f, t))
    return sorted(out, key=lambda x: -x[1])


def find_waste(root):
    """Return a list of findings: (severity, title, detail, fix)."""
    root = os.path.abspath(root)
    findings = []
    for p in _memory_files(root):
        t = _tok(open(p, encoding="utf-8", errors="replace").read())
        where = p.replace(os.path.expanduser("~"), "~")
        if t > MEMORY_WARN:
            findings.append(("high", "%s is %s tokens" % (where, format(t, ",")),
                             "It is sent with every request, so a 100-request session pays for it 100 times "
                             "(cheaper when cached, but it still fills the context).",
                             "Cut it to what most tasks need; move rarely used detail into separate docs Claude reads on demand."))
        else:
            findings.append(("ok", "%s is %s tokens" % (where, format(t, ",")), "", ""))

    settings = _settings(root)
    deny = " ".join(settings.get("deny", []))
    present_locks = [n for n in LOCKFILES if os.path.exists(os.path.join(root, n))]
    unguarded = [n for n in present_locks if n not in deny]
    if unguarded:
        findings.append(("medium", "Lockfiles Claude can read: %s" % ", ".join(unguarded),
                         "Lockfiles are huge and almost never useful to read.",
                         "headforward tune claude --apply (adds Read deny rules)"))
    dirs = [d for d in BUILD_DIRS if os.path.isdir(os.path.join(root, d)) and "./%s/" % d not in deny]
    if dirs:
        findings.append(("medium", "Build output Claude can read: %s" % ", ".join(d + "/" for d in dirs),
                         "Generated files waste tokens when an agent opens or searches them.",
                         "headforward tune claude --apply"))

    if "bashOutputMaxChars" not in settings:
        findings.append(("medium", "Command output size is not capped",
                         "A noisy test run or build can put tens of thousands of tokens into the conversation.",
                         "headforward tune claude --apply (sets bashOutputMaxChars=%d; longer output is saved to a "
                         "file Claude can still open)" % OUTPUT_CAP))

    for f, t in _big_files(root, BIG_FILE_TOKENS)[:8]:
        rel = os.path.relpath(f, root)
        findings.append(("high" if t > 4 * BIG_FILE_TOKENS else "medium",
                         "%s is %s tokens" % (rel, format(t, ",")),
                         "Each full read costs this much.",
                         "Point Claude at the part you mean, or add Read(./%s) to permissions.deny if it is generated." % rel))
    return findings


def cmd_audit(argv):
    ap = argparse.ArgumentParser(prog="headforward audit", description="Find what wastes Claude Code tokens in a project.")
    ap.add_argument("path", nargs="?", default=".", help="project folder (default: current folder)")
    a = ap.parse_args(argv)
    findings = find_waste(a.path)
    marks = {"high": "!!", "medium": "! ", "ok": "✓ "}
    print("headforward audit: %s\n" % os.path.abspath(a.path))
    for sev, title, detail, fix in sorted(findings, key=lambda x: ["high", "medium", "ok"].index(x[0])):
        print("%s %s" % (marks[sev], title))
        if detail:
            print("     %s" % detail)
        if fix:
            print("     fix: %s" % fix)
    issues = [f for f in findings if f[0] != "ok"]
    print("\n%s" % ("Nothing to fix." if not issues else
                    "%d thing%s to fix. Also: /clear between tasks, name exact files, ask for short answers."
                    % (len(issues), "" if len(issues) == 1 else "s")))
    return 0


def tune_plan(root):
    """The settings changes `tune claude` would make. Returns (path, new_data, added_rules, added_env)."""
    root = os.path.abspath(root)
    path = os.path.join(root, ".claude", "settings.local.json")
    try:
        with open(path) as f:
            data = json.load(f)
    except OSError:
        data = {}
    except ValueError:
        raise RuntimeError("%s is not valid JSON; fix it first" % path)
    existing = set(data.get("permissions", {}).get("deny", [])) | set(_settings(root).get("deny", []))
    rules = []
    for n in LOCKFILES:
        if os.path.exists(os.path.join(root, n)):
            rules.append("Read(./%s)" % n)
    for d in BUILD_DIRS:
        if os.path.isdir(os.path.join(root, d)):
            rules.append("Read(./%s/**)" % d)
    for g in GENERATED:
        rules.append("Read(./**/%s)" % g)
    rules = [r for r in rules if r not in existing]
    env_add = {}
    if "bashOutputMaxChars" not in data and "bashOutputMaxChars" not in _settings(root):
        env_add["bashOutputMaxChars"] = OUTPUT_CAP
    # Earlier versions set BASH_MAX_OUTPUT_LENGTH=30000, which only sizes a read-back window: remove it.
    stale = data.get("env", {}).get("BASH_MAX_OUTPUT_LENGTH") == "30000"
    new = json.loads(json.dumps(data))
    if rules:
        new.setdefault("permissions", {}).setdefault("deny", []).extend(rules)
    if env_add:
        new.update(env_add)
    if stale:
        del new["env"]["BASH_MAX_OUTPUT_LENGTH"]
        if not new["env"]:
            del new["env"]
        env_add["(removed old BASH_MAX_OUTPUT_LENGTH)"] = "-"
    return path, new, rules, env_add


def cmd_tune(argv):
    ap = argparse.ArgumentParser(prog="headforward tune",
                                 description="Apply token-saving Claude Code settings to a project.")
    ap.add_argument("agent", choices=["claude"])
    ap.add_argument("path", nargs="?", default=".", help="project folder (default: current folder)")
    ap.add_argument("--apply", action="store_true", help="write the changes (default: only show them)")
    a = ap.parse_args(argv)
    try:
        path, new, rules, env_add = tune_plan(a.path)
    except RuntimeError as e:
        print("headforward: %s" % e, file=sys.stderr)
        return 1
    if not rules and not env_add:
        print("Nothing to change: %s already has these settings." % path)
        return 0
    print("Changes for %s (your personal Claude Code settings for this project):" % path)
    for r in rules:
        print("  + deny %s" % r)
    for k, v in env_add.items():
        if k.startswith("(removed"):
            print("  - env BASH_MAX_OUTPUT_LENGTH  (had no effect; replaced by bashOutputMaxChars)")
        else:
            print("  + %s: %s  (characters of each command's output Claude reads inline; the rest is saved to a file)"
                  % (k, v))
    if not a.apply:
        print("\nNothing written. Run again with --apply to save. Restart Claude Code afterwards.")
        return 0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        shutil.copy2(path, path + ".bak")
    with open(path, "w") as f:
        json.dump(new, f, indent=2)
        f.write("\n")
    try:
        ignored = subprocess.run(["git", "-C", os.path.dirname(path), "check-ignore", "-q", path],
                                 capture_output=True, timeout=10).returncode == 0
        in_repo = subprocess.run(["git", "-C", os.path.dirname(path), "rev-parse"],
                                 capture_output=True, timeout=10).returncode == 0
        if in_repo and not ignored:
            print("Note: git does not ignore this file. Add .claude/settings.local.json to .gitignore "
                  "if you don't want to commit it.")
    except (OSError, subprocess.SubprocessError):
        pass
    print("\nSaved%s. Restart Claude Code to use them. To undo, delete those lines%s."
          % (" (backup at %s.bak)" % os.path.basename(path) if os.path.exists(path + ".bak") else "",
             " or restore the backup" if os.path.exists(path + ".bak") else ""))
    return 0


# ---------------------------------------------------------------------------
# usage: what Claude Code actually used, read from its own local logs.
# ---------------------------------------------------------------------------

CLAUDE_LOGS = os.path.join(os.path.expanduser("~"), ".claude", "projects")


def read_claude_usage(root=CLAUDE_LOGS, since=None):
    """One record per model reply: {day, model, project, input, cache_write, cache_read, output}.

    Claude Code writes a reply's usage on several log lines (one per content
    block); they share a message id and request id, so each reply counts once.
    """
    best = {}
    for dirpath, _, names in os.walk(root):
        for name in names:
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(dirpath, name)
            project = os.path.relpath(path, root).split(os.sep)[0]
            try:
                fh = open(path, encoding="utf-8", errors="replace")
            except OSError:
                continue
            with fh:
                for line in fh:
                    if '"usage"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    m = d.get("message")
                    if not isinstance(m, dict) or not isinstance(m.get("usage"), dict):
                        continue
                    ts = d.get("timestamp") or ""
                    if since and ts[:10] < since:
                        continue
                    u = m["usage"]
                    rec = {"day": ts[:10], "model": m.get("model") or "unknown", "project": project,
                           "input": u.get("input_tokens", 0) or 0,
                           "cache_write": u.get("cache_creation_input_tokens", 0) or 0,
                           "cache_read": u.get("cache_read_input_tokens", 0) or 0,
                           "output": u.get("output_tokens", 0) or 0}
                    key = (m.get("id"), d.get("requestId")) if m.get("id") else (path, ts)
                    if key not in best or rec["output"] >= best[key]["output"]:
                        best[key] = rec
    return [r for r in best.values() if r["model"] != "<synthetic>"]


def _usage_table(rows, key):
    groups = {}
    for r in rows:
        g = groups.setdefault(r[key], {"replies": 0, "input": 0, "cache_write": 0, "cache_read": 0, "output": 0})
        g["replies"] += 1
        for k in ("input", "cache_write", "cache_read", "output"):
            g[k] += r[k]
    return groups


def cmd_usage(argv):
    ap = argparse.ArgumentParser(prog="headforward usage",
                                 description="Claude Code token use from its local logs (~/.claude/projects).")
    ap.add_argument("--days", type=int, default=7, help="how many days back (default 7; 0 = all)")
    ap.add_argument("--by", choices=["day", "model", "project"], default="day")
    a = ap.parse_args(argv)
    since = None
    if a.days:
        since = time.strftime("%Y-%m-%d", time.localtime(time.time() - (a.days - 1) * 86400))
    rows = read_claude_usage(since=since)
    if not rows:
        print("No Claude Code usage found in %s%s." % (CLAUDE_LOGS, " for the last %d days" % a.days if a.days else ""))
        return 0
    groups = _usage_table(rows, a.by)
    f = lambda n: format(n, ",")
    width = max(12, max(len(str(k)) for k in groups) if a.by != "day" else 10)
    width = min(width, 48)
    hdr = "%-*s %8s %13s %13s %15s %11s %7s" % (width, a.by, "replies", "input", "cache write", "cache read",
                                              "output", "cached")
    print(hdr)
    print("-" * len(hdr))
    tot = {"replies": 0, "input": 0, "cache_write": 0, "cache_read": 0, "output": 0}
    for k in sorted(groups):
        g = groups[k]
        allin = g["input"] + g["cache_write"] + g["cache_read"]
        name = str(k)
        if a.by == "project":
            name = name.replace("-Users-" + os.path.basename(os.path.expanduser("~")), "~")[:width]
        print("%-*s %8s %13s %13s %15s %11s %6.0f%%" % (width, name, f(g["replies"]), f(g["input"]), f(g["cache_write"]),
                                                    f(g["cache_read"]), f(g["output"]),
                                                    100.0 * g["cache_read"] / allin if allin else 0))
        for x in tot:
            tot[x] += g[x]
    allin = tot["input"] + tot["cache_write"] + tot["cache_read"]
    print("-" * len(hdr))
    print("%-*s %8s %13s %13s %15s %11s %6.0f%%" % (width, "total", f(tot["replies"]), f(tot["input"]), f(tot["cache_write"]),
                                                f(tot["cache_read"]), f(tot["output"]),
                                                100.0 * tot["cache_read"] / allin if allin else 0))
    print("\ncached = share of input read from Anthropic's prompt cache (billed at about a tenth of normal input).")
    share = tot["cache_read"] / float(allin) if allin else 0
    per_reply = allin // max(1, tot["replies"])
    if share >= 0.9:
        print("Caching already works well. Each reply resent about %s tokens of conversation on average;\n"
              "shorter sessions (/clear, /compact) and `headforward wrap claude` shrink that." % f(per_reply))
    else:
        print("Only %.0f%% of input was cached. Uncached input costs about 10x more: keep CLAUDE.md and the start\n"
              "of each session stable, and avoid switching models mid-session." % (100 * share))
    return 0


# ---------------------------------------------------------------------------
# map: a compact outline of a codebase (idea from Aider's repo map).
# ---------------------------------------------------------------------------

_OUTLINE = {
    ".py": re.compile(r"^(\s*)(async\s+def|def|class)\s+(\w+)\s*(\([^)]*\)?)?"),
    ".js": re.compile(r"^(\s*)(export\s+)?(default\s+)?(async\s+)?(function\*?|class)\s+(\w+)\s*(\([^)]*\)?)?"
                      r"|^(\s*)(export\s+)?(const|let)\s+(\w+)\s*=\s*(async\s*)?(\([^)]*\)|\w+)\s*=>"),
    ".go": re.compile(r"^(func)\s+(\([^)]*\)\s*)?(\w+)\s*(\([^)]*\)?)|^(type)\s+(\w+)\s+(struct|interface)"),
    ".rs": re.compile(r"^(\s*)(pub(\([^)]*\))?\s+)?(async\s+)?(fn|struct|enum|trait|impl)\b\s*([^{;]*)"),
    ".java": re.compile(r"^(\s*)(public|private|protected)[\w\s<>\[\],]*?\b(class|interface|enum|record|\w+\s*\()"),
    ".rb": re.compile(r"^(\s*)(def|class|module)\s+([\w.:!?]+)"),
}
for _e in (".ts", ".tsx", ".jsx", ".mjs", ".cjs"):
    _OUTLINE[_e] = _OUTLINE[".js"]
for _e in (".kt", ".cs", ".swift", ".scala"):
    _OUTLINE[_e] = _OUTLINE[".java"]


def outline(path, text):
    """Signature lines of a source file, or None for unsupported files."""
    pat = _OUTLINE.get(os.path.splitext(path)[1].lower())
    if not pat:
        return None
    out = []
    for n, line in enumerate(text.split("\n"), 1):
        if pat.match(line):
            sig = line.rstrip().rstrip("{").rstrip(":").rstrip()
            if len(sig) > 140:
                sig = sig[:137] + "..."
            out.append("%5d  %s" % (n, sig))
    return out


def code_map(paths, budget=None):
    files, _ = _collect(paths)
    parts, used = [], 0
    for f in files:
        try:
            text = open(f, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        lines = outline(f, text)
        if lines is None or not lines:
            continue
        block = "%s\n%s" % (_rel(f), "\n".join(lines))
        cost = count(block)[0]
        if budget and used + cost > budget:
            parts.append("... (stopped at the %s-token budget)" % format(budget, ","))
            break
        used += cost
        parts.append(block)
    return "\n\n".join(parts)


def cmd_map(argv):
    ap = argparse.ArgumentParser(prog="headforward map",
                                 description="A compact outline of a codebase: every file's classes and functions with line numbers.")
    ap.add_argument("paths", nargs="*", default=["."])
    ap.add_argument("--max-tokens", type=int, help="stop at this many tokens")
    ap.add_argument("--copy", action="store_true", help="copy to the clipboard instead of printing")
    a = ap.parse_args(argv)
    text = code_map(a.paths, a.max_tokens)
    if not text:
        print("headforward: no supported source files found (Python, JS/TS, Go, Rust, Java-family, Ruby).", file=sys.stderr)
        return 1
    if a.copy:
        try:
            write_clipboard(text)
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            print("headforward: could not write the clipboard (%s)" % e, file=sys.stderr)
            return 1
    else:
        sys.stdout.write(text + "\n")
    print("headforward: map is %s tokens%s." % (format(count(text)[0], ","), ", copied" if a.copy else ""),
          file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# desktop: run the proxy in the background (macOS) and point Claude Code at it.
# Works for Claude Code in the desktop app, which `wrap` can't reach.
# ---------------------------------------------------------------------------

DESKTOP_LABEL = "com.headforward.proxy"
DESKTOP_PORT = 8787
DESKTOP_URL = "http://127.0.0.1:%d" % DESKTOP_PORT
DESKTOP_STATE = os.path.join(HOME_DIR, "desktop.json")
DESKTOP_PLIST = os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents", DESKTOP_LABEL + ".plist")
OLD_LABEL = "com.leantokens.proxy"  # the background proxy from before the rename
OLD_PLIST = os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents", OLD_LABEL + ".plist")


def _remove_old_service():
    """Stop and remove a background proxy installed under the old name; it holds the same port.

    Asks launchd rather than trusting the plist file: the service can still
    be loaded after its file is gone.
    """
    target = "gui/%d/%s" % (os.getuid(), OLD_LABEL)
    loaded = _launchctl("print", target).returncode == 0
    if not (loaded or os.path.exists(OLD_PLIST)):
        return False
    if loaded:
        _launchctl("bootout", target)
    if os.path.exists(OLD_PLIST):
        os.remove(OLD_PLIST)
    if loaded:
        for _ in range(40):  # wait for it to let go of the port
            if not _healthy(0.2):
                break
            time.sleep(0.25)
        else:
            raise RuntimeError("the old background proxy (%s) did not stop; run `launchctl bootout %s` "
                               "and try again" % (OLD_LABEL, target))
    return True


def _healthy(timeout=1.0):
    for path in ("/headforward/health", "/leantokens/health"):  # a proxy started before the rename
        try:
            with urllib.request.urlopen(DESKTOP_URL + path, timeout=timeout) as r:
                if json.loads(r.read()).get("ok") is True:
                    return True
        except (OSError, ValueError):
            pass
    return False


def _desktop_state():
    try:
        with open(DESKTOP_STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"settings": []}


def _save_desktop_state(state):
    os.makedirs(os.path.dirname(DESKTOP_STATE), exist_ok=True)
    with open(DESKTOP_STATE, "w") as f:
        json.dump(state, f, indent=2)


def _plist(mode_args):
    args = [sys.executable, os.path.abspath(__file__), "proxy", "--port", str(DESKTOP_PORT), "--quiet"] + mode_args
    log = os.path.join(HOME_DIR, "proxy.log")
    items = "".join("    <string>%s</string>\n" % a.replace("&", "&amp;").replace("<", "&lt;") for a in args)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0">\n<dict>\n'
            '  <key>Label</key><string>%s</string>\n'
            '  <key>ProgramArguments</key>\n  <array>\n%s  </array>\n'
            '  <key>RunAtLoad</key><true/>\n'
            '  <key>KeepAlive</key><true/>\n'
            '  <key>ThrottleInterval</key><integer>1</integer>\n'
            '  <key>StandardErrorPath</key><string>%s</string>\n'
            '  <key>StandardOutPath</key><string>%s</string>\n'
            '</dict>\n</plist>\n') % (DESKTOP_LABEL, items, log, log)


def _launchctl(*args):
    return subprocess.run(["launchctl"] + list(args), capture_output=True, text=True, timeout=30)


def _settings_path(target):
    if target == "global":
        return os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
    return os.path.join(os.path.abspath(os.path.expanduser(target)), ".claude", "settings.local.json")


def _read_settings(path):
    try:
        with open(path) as f:
            return json.load(f)
    except OSError:
        return {}
    except ValueError:
        raise RuntimeError("%s is not valid JSON; fix it first" % path)


def _write_settings(path, data):
    if not data.get("env"):
        data.pop("env", None)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        shutil.copy2(path, path + ".bak")
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def _old_https_env():
    """What `desktop on --https` wrote before the rename; still ours to replace or remove."""
    return dict(_https_env(), NODE_EXTRA_CA_CERTS=os.path.join(OLD_HOME, "ca", "ca.pem"))


def _https_env():
    return {"HTTPS_PROXY": DESKTOP_URL, "NODE_EXTRA_CA_CERTS": _ca_paths()["cert"],
            "NO_PROXY": "localhost,127.0.0.1,::1"}


def _set_https_env(path, on):
    """Add (on) or remove the settings that route the desktop app through the proxy as its HTTPS_PROXY.

    The desktop app pins ANTHROPIC_BASE_URL, but Claude Code still honors
    HTTPS_PROXY and NODE_EXTRA_CA_CERTS from settings.
    """
    data = _read_settings(path)
    env = data.setdefault("env", {})
    want, old = _https_env(), _old_https_env()
    if on:
        for k in ("HTTPS_PROXY", "NODE_EXTRA_CA_CERTS"):
            if env.get(k) not in (None, want[k], old[k]):
                raise RuntimeError("%s already sets %s=%s; leaving it alone" % (path, k, env[k]))
        env.update({k: v for k, v in want.items() if k != "NO_PROXY" or k not in env})
    else:
        mine = [k for k in want if env.get(k) in (want[k], old[k])]
        if not mine:
            return False
        for k in mine:
            del env[k]
    _write_settings(path, data)
    return True


def _set_base_url(path, url, previous=None):
    """Set (url) or remove (None) ANTHROPIC_BASE_URL in a Claude Code settings file.

    previous is a gateway URL the background proxy forwards to: setting may
    replace it, and removing puts it back.
    """
    data = _read_settings(path)
    env = data.setdefault("env", {})
    if url:
        if env.get("ANTHROPIC_BASE_URL") not in (None, url, previous):
            raise RuntimeError("%s already sets ANTHROPIC_BASE_URL=%s; leaving it alone" % (path, env["ANTHROPIC_BASE_URL"]))
        env["ANTHROPIC_BASE_URL"] = url
    else:
        if env.get("ANTHROPIC_BASE_URL") != DESKTOP_URL:
            return False
        if previous:
            env["ANTHROPIC_BASE_URL"] = previous
        else:
            del env["ANTHROPIC_BASE_URL"]
    _write_settings(path, data)
    return True


def desktop_on(target, mode_args):
    if sys.platform != "darwin":
        raise RuntimeError("`headforward desktop` uses macOS launchd; on other systems run `headforward proxy` yourself")
    path = _settings_path(target)
    state = _desktop_state()
    chained = state.get("upstream")
    current = _get_base_url(path)
    if current and current.rstrip("/") != DESKTOP_URL:
        # Another gateway is set (a company or routing proxy): sit in front of it.
        if chained and chained != current:
            raise RuntimeError("%s sets ANTHROPIC_BASE_URL=%s, but the background proxy already forwards to %s; "
                               "leaving it alone" % (path, current, chained))
        chained = current
    https = "--https" in mode_args
    if https:
        ensure_ca()  # before launchd starts the proxy, so the two never race to create it
        env = _read_settings(path).get("env") or {}
        for k, v in _https_env().items():
            if k != "NO_PROXY" and env.get(k) not in (None, v, _old_https_env()[k]):
                raise RuntimeError("%s already sets %s=%s; leaving it alone" % (path, k, env[k]))
    os.makedirs(os.path.dirname(DESKTOP_PLIST), exist_ok=True)
    with open(DESKTOP_PLIST, "w") as f:
        f.write(_plist(mode_args + (["--upstream", chained] if chained else [])))
    domain = "gui/%d" % os.getuid()
    _remove_old_service()
    _launchctl("bootout", domain + "/" + DESKTOP_LABEL)
    for _ in range(10):  # bootout finishes asynchronously; bootstrap fails (error 5) until it has
        r = _launchctl("bootstrap", domain, DESKTOP_PLIST)
        if r.returncode == 0:
            break
        time.sleep(0.5)
    for _ in range(50):
        if _healthy(0.5):
            break
        time.sleep(0.2)
    else:
        raise RuntimeError("the background proxy did not start (launchctl: %s). See ~/.headforward/proxy.log"
                           % (r.stderr.strip() or "no error"))
    _set_base_url(path, DESKTOP_URL, chained)
    for p in [path] + state["settings"]:
        _set_https_env(p, https)  # also drops it from settings when turned on again without --https
    if path not in state["settings"]:
        state["settings"].append(path)
    if current and current.rstrip("/") != DESKTOP_URL:
        state.setdefault("previous", {})[path] = current
    if chained:
        state["upstream"] = chained
    state["mode"] = mode_args
    _save_desktop_state(state)
    return path


def desktop_off():
    state = _desktop_state()
    previous = state.get("previous", {})
    changed = [p for p in state.get("settings", [])
               if [_set_base_url(p, None, previous.get(p)), _set_https_env(p, False)] != [False, False]]
    if sys.platform == "darwin":
        _launchctl("bootout", "gui/%d/%s" % (os.getuid(), DESKTOP_LABEL))
        _remove_old_service()
    if os.path.exists(DESKTOP_PLIST):
        os.remove(DESKTOP_PLIST)
    _save_desktop_state({"settings": []})
    return changed


def cmd_desktop(argv):
    ap = argparse.ArgumentParser(prog="headforward desktop",
                                 description="Compress Claude Code sessions from the desktop app (macOS).")
    ap.add_argument("action", choices=["on", "off", "status"])
    ap.add_argument("target", nargs="?", default=".",
                    help="project folder to turn on (default: current folder), or `global` for every project")
    ap.add_argument("--smart", action="store_true", help="use smart mode in the background proxy")
    ap.add_argument("--https", action="store_true",
                    help="also reach Claude Code in the desktop app, which ignores base URLs, "
                         "by acting as its HTTPS proxy (decrypts api.anthropic.com with a local CA)")
    a = ap.parse_args(argv)
    if a.action == "on":
        try:
            path = desktop_on(a.target, (["--smart"] if a.smart else []) + (["--https"] if a.https else []))
        except RuntimeError as e:
            print("headforward: %s" % e, file=sys.stderr)
            return 1
        up = _desktop_state().get("upstream")
        print("On. The background proxy runs at %s and starts at login.\n"
              "Claude Code now uses it via %s.\n" % (DESKTOP_URL, path)
              + ("Chain: Claude Code -> headforward -> %s\n" % up if up else "")
              + ("HTTPS mode: the desktop app reaches it via HTTPS_PROXY, trusting %s.\n"
                 "If the proxy is down, those sessions can't connect: run `headforward desktop off`.\n"
                 % _ca_paths()["cert"] if a.https else "") +
              "Start a new Claude Code session (existing ones keep their old settings).\n"
              "Undo everything: headforward desktop off")
        return 0
    if a.action == "off":
        changed = desktop_off()
        print("Off. Background proxy stopped and removed.%s" % (
            "" if not changed else "\nRestored ANTHROPIC_BASE_URL (and any HTTPS_PROXY it added) in:\n  "
            + "\n  ".join(changed)))
        print("Start a new Claude Code session to go back to connecting directly.")
        return 0
    state = _desktop_state()
    up = _healthy()
    print("background proxy: %s" % ("running at %s" % DESKTOP_URL if up else
                                     "NOT running" + (" (installed but down: Claude Code sessions pointed at it "
                                                      "will fail; run `headforward desktop on` or `off`)"
                                                      if os.path.exists(DESKTOP_PLIST) else "")))
    print("mode: %s" % (" ".join(state.get("mode") or []) or "lossless"))
    if state.get("upstream"):
        print("forwards to: %s" % state["upstream"])
    for p in state.get("settings", []):
        print("used by: %s" % p)
    if not state.get("settings"):
        print("used by: nothing (run `headforward desktop on PROJECT` or `on global`)")
    return 0 if (up or not state.get("settings")) else 1


# ---------------------------------------------------------------------------
# hooks: compress noisy command output inside Claude Code itself.
# A PreToolUse hook rewrites test/install/build commands to pipe their output
# through `headforward filter`. Works in the desktop app and the terminal alike,
# because it runs inside Claude Code rather than on the network path.
# ---------------------------------------------------------------------------

# Commands whose output is mostly routine noise. Only these are touched.
NOISY = re.compile(
    r"^(?:[A-Z_][A-Z0-9_]*=\S*\s+)*(?:"
    r"(?:npm|pnpm|yarn|bun)\s+(?:install|i|ci|add|test|t|run\s+(?:test|build|lint)\S*|build)\b"
    r"|npx\s+(?:jest|vitest|tsc|eslint|playwright|mocha)\b"
    r"|(?:jest|vitest|mocha|tsc|eslint|rspec|phpunit)\b"
    r"|pytest\b|python3?\s+-m\s+(?:pytest|unittest|pip\s+install)\b"
    r"|pip3?\s+install\b|uv\s+(?:pip\s+install|sync|add)\b|poetry\s+install\b"
    r"|go\s+(?:test|build|mod\s+(?:download|tidy))\b"
    r"|cargo\s+(?:build|test|check|clippy|install)\b"
    r"|make\b|mvn\b|\./mvnw\b|gradle\b|\./gradlew\b"
    r"|docker\s+(?:build|pull|compose\s+(?:build|pull|up))\b"
    r"|bundle\s+install\b|composer\s+install\b|brew\s+(?:install|upgrade)\b"
    r"|git\s+log\b|ls\s+-[a-zA-Z]*l|grep\s|rg\s|log\s+show\b"
    r")")
UNSAFE = re.compile(r"<<|(^|[^&|])&\s*$|\bcd\s|(?:headforward|leantokens)(?:\.py)?\s+(?:filter|hook-bash)\b"
                    r"|\|\s*(?:tee|less|more|head|tail)\b")


def filter_output(text):
    """Conservative cleanup for command output: colors, routine runs, repeats.

    Error-like lines are always kept; anything removed is saved and readable
    with `headforward show ID`.
    """
    s = ANSI.sub("", text).replace("\r\n", "\n")
    s = "\n".join(l.split("\r")[-1] for l in s.split("\n"))  # progress bars redraw with \r
    s = _compact_shapes(s)
    s = _filter_noise(s)
    lines = [l.rstrip() for l in s.split("\n")]
    out, prev, n = [], None, 0
    for line in lines + [None]:
        if line == prev:
            n += 1
            continue
        if prev is not None:
            out.extend([prev] if n == 1 else ([prev] * n if n < 3 else ["%s  [x%d]" % (prev, n)]))
        prev, n = line, 1
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out))


def cmd_filter(argv):
    data = sys.stdin.buffer.read().decode("utf-8", "replace")
    out = filter_output(data)
    sys.stdout.write(out if out.endswith("\n") or not out else out + "\n")
    if len(out) < len(data):
        _append_stats({"t": int(time.time()), "p": "hook", "b": len(data), "a": len(out)})
    return 0


def _self_cmd():
    """How a hook should call this exact headforward, independent of PATH."""
    return "%s %s" % (shlex.quote(sys.executable), shlex.quote(os.path.abspath(__file__)))


def rewrite_command(cmd):
    """The command wrapped to filter its output, or None to leave it alone."""
    c = cmd.strip()
    if not c or "\n" in c or not NOISY.match(c) or UNSAFE.search(c):
        return None
    # A subshell keeps pipefail from leaking into later commands; the command's
    # own exit code is preserved, so failures still look like failures.
    return "( set -o pipefail; { %s\n} 2>&1 | %s filter )" % (c, _self_cmd())


def cmd_hook_bash(argv):
    """PreToolUse hook for Bash. Prints nothing (no change) unless it rewrites."""
    try:
        event = json.loads(sys.stdin.read() or "{}")
        tool_input = event.get("tool_input") or {}
        new = rewrite_command(tool_input.get("command") or "")
        if new:
            updated = dict(tool_input, command=new)
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": updated}}))
    except Exception as e:  # a hook must never break the agent
        _log_error("hook-bash: %r" % e)
    return 0


HOOK_MARK = " hook-bash"


def _is_our_hook(command):
    return HOOK_MARK in command and ("headforward" in command or "leantokens" in command)


def _hook_settings_path(target):
    return _settings_path(target)


def hooks_install(target):
    path = _hook_settings_path(target)
    try:
        with open(path) as f:
            data = json.load(f)
    except OSError:
        data = {}
    except ValueError:
        raise RuntimeError("%s is not valid JSON; fix it first" % path)
    pre = data.setdefault("hooks", {}).setdefault("PreToolUse", [])
    ours = [h for group in pre for h in group.get("hooks", []) if _is_our_hook(h.get("command", ""))]
    if ours:
        if all(h["command"] == _self_cmd() + HOOK_MARK for h in ours):
            return path, False
        for h in ours:  # installed under the old name or from another copy: point it here
            h["command"] = _self_cmd() + HOOK_MARK
    else:
        pre.append({"matcher": "Bash",
                    "hooks": [{"type": "command", "command": _self_cmd() + HOOK_MARK, "timeout": 10}]})
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        shutil.copy2(path, path + ".bak")
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    return path, True


def hooks_uninstall(target):
    path = _hook_settings_path(target)
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return path, False
    if not isinstance(data.get("hooks"), dict):
        return path, False
    pre = data["hooks"].get("PreToolUse", [])
    removed = False
    for group in pre:
        keep = [h for h in group.get("hooks", [])
                if not _is_our_hook(h.get("command", ""))]
        removed |= len(keep) != len(group.get("hooks", []))
        group["hooks"] = keep
    data["hooks"]["PreToolUse"] = [g for g in pre if g.get("hooks")] if pre else pre
    if not data["hooks"].get("PreToolUse"):
        data["hooks"].pop("PreToolUse", None)
    if not data["hooks"]:
        data.pop("hooks")
    if removed:
        shutil.copy2(path, path + ".bak")
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
    return path, removed


def cmd_hooks(argv):
    ap = argparse.ArgumentParser(prog="headforward hooks",
                                 description="Compress noisy command output inside Claude Code (desktop app and terminal).")
    ap.add_argument("action", choices=["install", "uninstall"])
    ap.add_argument("target", nargs="?", default=".", help="project folder (default: current), or `global`")
    a = ap.parse_args(argv)
    try:
        path, changed = (hooks_install if a.action == "install" else hooks_uninstall)(a.target)
    except RuntimeError as e:
        print("headforward: %s" % e, file=sys.stderr)
        return 1
    if a.action == "install":
        print(("Installed in %s.\nNew Claude Code sessions there will filter test, install and build output.\n"
               "Undo: headforward hooks uninstall %s" if changed else "Already installed in %s.") % (
                  (path, a.target) if changed else (path,)))
    else:
        print(("Removed from %s." if changed else "Nothing to remove in %s.") % path)
    return 0


USAGE = """usage:
  headforward wrap claude [args...]       run Claude Code through the compressor
  headforward wrap codex [args...]        run OpenAI Codex CLI (API key auth)
  headforward wrap aider [args...]        run Aider
  headforward proxy [--port 8787]         run the proxy for any app or SDK
  headforward stats                       real billed tokens and estimated savings
  headforward eval FOLDER [--tokens-only] replay recorded requests, compare savings and answers
  headforward show ID [--lines A-B]       print an output that --smart shortened

Claude chat (claude.ai, desktop app):
  headforward clip                        compress the clipboard in place, then paste
  headforward pack PATH... [--max-tokens N]  bundle files into one compact block for Claude
  headforward count FILE                  Claude tokens before and after (exact with ANTHROPIC_API_KEY)
  headforward doctor                      check your Claude setup and say what to fix

Claude Code habits:
  headforward audit [PATH]                find what wastes tokens in a project
  headforward tune claude [PATH] --apply  add safe token-saving Claude Code settings
  headforward usage [--days N] [--by day|model|project]  Claude Code token use from its local logs
  headforward map [PATH...] [--copy]      compact outline of a codebase (classes, functions, lines)

Claude Code anywhere, including the desktop app:
  headforward hooks install [PROJECT|global]  filter test/install/build output via a Claude Code hook
  headforward hooks uninstall [PROJECT|global]

Always-on proxy for terminal Claude Code (macOS):
  headforward desktop on [PROJECT|global] run the proxy in the background and point Claude Code at it
  headforward desktop on global --https    also reach Claude Code in the desktop app (HTTPS_PROXY)
  headforward desktop status              is it running, and which settings use it
  headforward desktop off                 stop it and undo every settings change
  headforward prompt.txt [-l high]        compress a prompt file (- for stdin)

wrap and proxy options:
  --smart           also reference lines already shown earlier (re-opened files) and
                    shorten outputs over 400 lines; the full text stays readable with
                    `headforward show ID --lines A-B`
  --lossy           also shorten runs of near-identical log lines (keeps first and last)
  --cache           Anthropic: add prompt-caching markers when an app doesn't set any
  --record FOLDER   save original requests (no API keys) for `headforward eval`
"""


def _env(name):
    """HEADFORWARD_<name>, or LEANTOKENS_<name> from before the rename."""
    return os.environ.get("HEADFORWARD_" + name) or os.environ.get("LEANTOKENS_" + name)


def _migrate_home():
    """Move ~/.leantokens to ~/.headforward, leaving a link so old paths in settings keep working."""
    if os.path.lexists(HOME_DIR) or not os.path.isdir(OLD_HOME) or os.path.islink(OLD_HOME):
        return
    try:
        os.rename(OLD_HOME, HOME_DIR)
        os.symlink(HOME_DIR, OLD_HOME)
    except OSError as e:  # symlinks can need extra rights on Windows: fall back to the old folder
        if os.path.isdir(HOME_DIR) and not os.path.exists(OLD_HOME):
            os.rename(HOME_DIR, OLD_HOME)
        print("headforward: could not move %s to %s (%s); still using the old folder" % (OLD_HOME, HOME_DIR, e),
              file=sys.stderr)
        _use_home(OLD_HOME)


def _use_home(path):
    global HOME_DIR, BLOB_DIR, STATS_FILE, CA_DIR, DESKTOP_STATE
    HOME_DIR = path
    BLOB_DIR, STATS_FILE = os.path.join(path, "outputs"), os.path.join(path, "stats.jsonl")
    CA_DIR, DESKTOP_STATE = os.path.join(path, "ca"), os.path.join(path, "desktop.json")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    _migrate_home()
    if argv and argv[0] in ("-h", "--help", "help"):
        print(USAGE + __doc__)
        return 0
    if argv and argv[0] == "wrap":
        return cmd_wrap(argv[1:], _env("UPSTREAM"))
    if argv and argv[0] == "stats":
        return cmd_stats()
    if argv and argv[0] == "eval":
        return cmd_eval(argv[1:])
    if argv and argv[0] == "show":
        return cmd_show(argv[1:])
    if argv and argv[0] in ("doctor", "clip", "pack", "count", "audit", "tune", "usage", "map", "desktop",
                                 "hooks", "hook-bash", "filter"):
        return {"doctor": cmd_doctor, "clip": cmd_clip, "pack": cmd_pack, "count": cmd_count,
                "audit": cmd_audit, "tune": cmd_tune, "usage": cmd_usage, "map": cmd_map, "desktop": cmd_desktop, "hooks": cmd_hooks,
                "hook-bash": cmd_hook_bash, "filter": cmd_filter}[argv[0]](argv[1:])
    if argv and argv[0] == "proxy":
        ap = argparse.ArgumentParser(prog="headforward proxy")
        ap.add_argument("--port", type=int, default=8787)
        ap.add_argument("--upstream", help="send everything to this base URL instead")
        ap.add_argument("--lossy", action="store_true", help="also shorten runs of near-identical log lines")
        ap.add_argument("--smart", action="store_true", help="reference repeated lines, shorten huge outputs")
        ap.add_argument("--cache", action="store_true", help="add Anthropic prompt-caching markers")
        ap.add_argument("--record", metavar="FOLDER", help="save original requests for `headforward eval`")
        ap.add_argument("--quiet", action="store_true", help="don't print a line per request")
        ap.add_argument("--https", action="store_true",
                        help="also act as an HTTPS_PROXY, decrypting api.anthropic.com with a local CA")
        a = ap.parse_args(argv[1:])

        def log(row):
            print("[%s] %s" % (row["p"], _fmt_summary(summarize([row]))), file=sys.stderr, flush=True)

        own = ("127.0.0.1:%d" % a.port, "localhost:%d" % a.port)
        for prov, base in list(UPSTREAMS.items()):
            if urllib.parse.urlsplit(base).netloc in own:  # would forward to itself forever
                UPSTREAMS[prov] = {"anthropic": "https://api.anthropic.com", "openai": "https://api.openai.com"}[prov]
        try:
            srv = start_proxy(a.port, a.upstream, None if a.quiet else log, a.lossy, a.record, a.smart, a.cache,
                              a.https)
        except RuntimeError as e:
            print("headforward: %s" % e, file=sys.stderr)
            return 1
        url = "http://127.0.0.1:%d" % srv.server_address[1]
        print("headforward proxy on %s\n  Anthropic: ANTHROPIC_BASE_URL=%s\n  OpenAI:    OPENAI_BASE_URL=%s/v1"
              % (url, url, url), file=sys.stderr, flush=True)
        if a.https:
            print("  HTTPS:     HTTPS_PROXY=%s NODE_EXTRA_CA_CERTS=%s" % (url, _ca_paths()["cert"]),
                  file=sys.stderr, flush=True)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            srv.shutdown()
        return 0
    if argv and argv[0] == "compress":
        argv = argv[1:]

    ap = argparse.ArgumentParser(prog="headforward", description="Shrink prompts before sending them to an LLM.")
    ap.add_argument("file", nargs="?", default="-", help="file to compress, or - for stdin")
    ap.add_argument("-l", "--level", default="medium", choices=LEVELS)
    ap.add_argument("-s", "--stats", action="store_true", help="print savings only")
    ap.add_argument("-q", "--quiet", action="store_true", help="no savings line on stderr")
    ap.add_argument("-t", "--tool", action="store_true", help="treat input as tool output (JSON, logs)")
    ap.add_argument("--lossy", action="store_true", help="with --tool: shorten near-identical log lines")
    ap.add_argument("--model", default="gpt-4o", help="tokenizer model for tiktoken")
    a = ap.parse_args(argv)

    text = sys.stdin.read() if a.file == "-" else open(a.file, encoding="utf-8").read()
    out = compress_tool(text, a.lossy) if a.tool else compress(text, a.level)
    before, exact = count(text, a.model)
    after, _ = count(out, a.model)
    saved = before - after
    pct = 100 * saved / before if before else 0
    line = "%d -> %d tokens, saved %d (%.1f%%)%s" % (
        before, after, saved, pct, "" if exact else " (estimate; pip install tiktoken for exact)")
    if a.stats:
        print(line)
        return 0
    sys.stdout.write(out + "\n")
    if not a.quiet:
        print(line, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
