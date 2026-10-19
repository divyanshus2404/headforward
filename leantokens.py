#!/usr/bin/env python3
"""leantokens: cut the tokens Claude, ChatGPT and coding agents spend.

Single file, zero dependencies, Python 3.9+. Install `tiktoken` for exact counts.

Coding agents (biggest savings: compresses tool output, JSON, logs, file reads):
  leantokens wrap claude            # Claude Code
  leantokens wrap codex             # OpenAI Codex CLI (API key auth)
  leantokens proxy --port 8787      # any app or SDK: set its base URL
  leantokens stats                  # tokens saved so far

Prompts:
  leantokens prompt.txt -l high
  pbpaste | leantokens - -l high | pbcopy
  leantokens api.json --tool        # tool output: JSON tables, log repeats

Prompt levels:
  light   whitespace and blank lines
  medium  + filler words, wordy phrases, greetings and sign-offs, repeated sentences
  high    + articles, hedges, markdown emphasis, minify JSON code blocks
  max     + strip comment-only lines and blank lines inside code blocks
"""
import argparse
import difflib
import hashlib
import http.client
import http.server
import json
import os
import re
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


def _tool_results(body):
    """Yield (container, key, call_id) for every tool result, in conversation order."""
    for m in body.get("messages") or []:
        if not isinstance(m, dict):
            continue
        if m.get("role") == "tool":  # OpenAI chat completions
            yield m, "content", m.get("tool_call_id")
        elif isinstance(m.get("content"), list):  # Anthropic
            for block in m["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    yield block, "content", block.get("tool_use_id")
    items = body.get("input")
    if isinstance(items, list):  # OpenAI Responses API
        for it in items:
            if isinstance(it, dict) and it.get("type") in ("function_call_output", "custom_tool_call_output"):
                yield it, "output", it.get("call_id")


REF_MIN_RUN = 6          # shortest run of repeated lines worth a reference
BIG_LINES = 400          # outputs longer than this become expandable in smart mode
BLOB_DIR = os.path.join(os.path.expanduser("~"), ".leantokens", "outputs")
_ERRORISH = re.compile(r"error|exception|traceback|fail|warn|fatal|panic|assert", re.I)


def _reference_runs(out, call_id, index):
    """Replace runs of lines already shown in an earlier tool result with a reference.

    index maps line text -> (call_id, line_no) for lines the model has already seen.
    A run is only replaced when it is contiguous in both places, so the reference
    points at one exact block. Lines are added to the index as they are sent.
    """
    lines = out.split("\n")
    res, i = [], 0
    while i < len(lines):
        src = index.get(lines[i]) if len(lines[i].strip()) > 3 else None
        j = i + 1
        if src and src[0] != call_id:
            while (j < len(lines) and index.get(lines[j]) == (src[0], src[1] + (j - i))):
                j += 1
        if src and src[0] != call_id and j - i >= REF_MIN_RUN:
            res.append("[%d lines identical to lines %d-%d of tool call %s]"
                       % (j - i, src[1] + 1, src[1] + j - i, src[0]))
        else:
            res.extend(lines[i:j])
        i = j
    for n, line in enumerate(res):
        if len(line.strip()) > 3:
            index.setdefault(line, (call_id, n))
    return "\n".join(res)


def _expandable(text):
    """Shorten a very long output to its start, end and error lines.

    The full text is saved under ~/.leantokens/outputs so the agent can read any
    part of it with `leantokens show ID --lines A-B`.
    """
    lines = text.split("\n")
    if len(lines) <= BIG_LINES:
        return text
    blob = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:12]
    try:
        os.makedirs(BLOB_DIR, exist_ok=True)
        path = os.path.join(BLOB_DIR, blob + ".txt")
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
    except OSError:
        return text
    head, tail = 80, 40
    middle = [(n, l) for n, l in enumerate(lines[head:-tail], head) if _ERRORISH.search(l)][:40]
    out = lines[:head]
    out.append("[leantokens: %d of %d lines omitted. Error-like lines from the omitted part follow. "
               "To read any part, run: leantokens show %s --lines A-B]" % (len(lines) - head - tail, len(lines), blob))
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
         saved locally and readable with `leantokens show`.
    lossy=True also shortens runs of near-identical log lines.

    Earlier turns never change and the output is deterministic, so prompt caches
    keep hitting. Returns (chars_before, chars_after). User text is untouched.
    """
    stats = [0, 0]
    seen = {}
    index = {}

    for holder, key, call_id in _tool_results(body):
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
                        out = _reference_runs(_expandable(out), call_id, index)
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
STATS_FILE = os.path.join(os.path.expanduser("~"), ".leantokens", "stats.jsonl")
HOP = {"host", "content-length", "accept-encoding", "connection", "keep-alive",
       "transfer-encoding", "proxy-connection", "upgrade", "te"}
SECRET_HEADERS = {"authorization", "x-api-key", "api-key", "cookie", "openai-organization", "openai-project"}
MAX_CAPTURE = 8 * 1024 * 1024


def _append_stats(row):
    try:
        os.makedirs(os.path.dirname(STATS_FILE), exist_ok=True)
        with open(STATS_FILE, "a") as f:
            f.write(json.dumps(row) + "\n")
    except OSError:
        pass


def _save_recording(folder, provider, path, headers, body):
    """Save an original (uncompressed) request for `leantokens eval`. No secrets."""
    try:
        os.makedirs(folder, exist_ok=True)
        name = "%d-%s.json" % (time.time() * 1000, hashlib.sha1(body).hexdigest()[:8])
        keep = {k: v for k, v in headers.items() if k.lower() not in SECRET_HEADERS | HOP}
        with open(os.path.join(folder, name), "w") as f:
            json.dump({"provider": provider, "path": path, "headers": keep,
                       "body": json.loads(body)}, f)
    except (OSError, ValueError):
        pass


def make_handler(upstream=None, log=None, lossy=False, record=None, smart=False, cache=False):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _forward(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else b""
            anthropic = self.path.startswith("/v1/messages") or "anthropic-version" in self.headers
            provider = "anthropic" if anthropic else "openai"
            base = urllib.parse.urlsplit(upstream or UPSTREAMS[provider])
            row = None

            is_json = body and "json" in (self.headers.get("Content-Type") or "")
            if is_json and record and self.command == "POST":
                _save_recording(record, provider, self.path, dict(self.headers.items()), body)
            if is_json:
                try:
                    data = json.loads(body)
                    before, after = compress_request(data, lossy, smart)
                    cached = cache and provider == "anthropic" and add_cache_control(data)
                    if before > after or cached:
                        body = json.dumps(data, ensure_ascii=False).encode()
                    if before:
                        row = {"t": int(time.time()), "p": provider, "b": before, "a": after,
                               "req": len(body)}
                except ValueError:
                    pass

            headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
            headers["Content-Length"] = str(len(body))
            Conn = http.client.HTTPSConnection if base.scheme == "https" else http.client.HTTPConnection
            try:
                conn = Conn(base.netloc, timeout=600)
                conn.request(self.command, base.path.rstrip("/") + self.path, body or None, headers)
                resp = conn.getresponse()
            except OSError as e:
                msg = ("leantokens: could not reach %s (%s)" % (base.netloc, e)).encode()
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

    return Handler


def start_proxy(port=0, upstream=None, log=None, lossy=False, record=None, smart=False, cache=False):
    handler = make_handler(upstream, log, lossy, record, smart, cache)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
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
    s = {"requests": len(rows), "in": 0, "cached": 0, "out": 0, "saved": 0.0, "measured": 0}
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


def cmd_wrap(argv, upstream=None):
    lossy, argv = _flag(argv, "--lossy")
    smart, argv = _flag(argv, "--smart")
    cache, argv = _flag(argv, "--cache")
    record, argv = _option(argv, "--record")
    if not argv:
        print(USAGE, file=sys.stderr)
        return 2
    agent, args = argv[0], argv[1:]
    rows = []
    srv = start_proxy(upstream=upstream, log=rows.append, lossy=lossy or bool(os.environ.get("LEANTOKENS_LOSSY")),
                      record=record, smart=smart or bool(os.environ.get("LEANTOKENS_SMART")), cache=cache)
    url = "http://127.0.0.1:%d" % srv.server_address[1]
    var, suffix = AGENTS.get(agent, ("OPENAI_BASE_URL", "/v1"))
    env = dict(os.environ)
    env[var] = url + suffix
    if agent == "aider":
        env["ANTHROPIC_BASE_URL"] = url
    try:
        code = subprocess.call([agent] + args, env=env)
    except FileNotFoundError:
        print("leantokens: '%s' is not installed or not on your PATH" % agent, file=sys.stderr)
        return 127
    except KeyboardInterrupt:
        code = 130
    srv.shutdown()
    if rows:
        print("leantokens: this session %s" % _fmt_summary(summarize(rows)), file=sys.stderr)
    return code


def cmd_stats():
    rows = []
    try:
        with open(STATS_FILE) as f:
            rows = [json.loads(l) for l in f if l.strip()]
    except (OSError, ValueError):
        pass
    if not rows:
        print("No savings recorded yet. Run an agent through `leantokens wrap claude` first.")
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
    ap = argparse.ArgumentParser(prog="leantokens eval",
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
    ap = argparse.ArgumentParser(prog="leantokens show", description="Print an output that --smart shortened.")
    ap.add_argument("id", help="the ID from the [leantokens: ... omitted] note")
    ap.add_argument("--lines", help="line range, e.g. 120-180")
    a = ap.parse_args(argv)
    if not re.fullmatch(r"[0-9a-f]{12}", a.id):
        print("leantokens: not a valid output ID: %s" % a.id, file=sys.stderr)
        return 2
    try:
        lines = open(os.path.join(BLOB_DIR, a.id + ".txt"), encoding="utf-8").read().split("\n")
    except OSError:
        print("leantokens: no saved output with ID %s" % a.id, file=sys.stderr)
        return 1
    lo, hi = 1, len(lines)
    if a.lines:
        m = re.fullmatch(r"(\d+)-(\d+)", a.lines)
        if not m:
            print("leantokens: --lines must look like 120-180", file=sys.stderr)
            return 2
        lo, hi = max(1, int(m.group(1))), min(len(lines), int(m.group(2)))
    for n in range(lo, hi + 1):
        print("%d: %s" % (n, lines[n - 1]))
    return 0


USAGE = """usage:
  leantokens wrap claude [args...]       run Claude Code through the compressor
  leantokens wrap codex [args...]        run OpenAI Codex CLI (API key auth)
  leantokens wrap aider [args...]        run Aider
  leantokens proxy [--port 8787]         run the proxy for any app or SDK
  leantokens stats                       real billed tokens and estimated savings
  leantokens eval FOLDER [--tokens-only] replay recorded requests, compare savings and answers
  leantokens show ID [--lines A-B]       print an output that --smart shortened
  leantokens prompt.txt [-l high]        compress a prompt file (- for stdin)

wrap and proxy options:
  --smart           also reference lines already shown earlier (re-opened files) and
                    shorten outputs over 400 lines; the full text stays readable with
                    `leantokens show ID --lines A-B`
  --lossy           also shorten runs of near-identical log lines (keeps first and last)
  --cache           Anthropic: add prompt-caching markers when an app doesn't set any
  --record FOLDER   save original requests (no API keys) for `leantokens eval`
"""


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ("-h", "--help", "help"):
        print(USAGE + __doc__)
        return 0
    if argv and argv[0] == "wrap":
        return cmd_wrap(argv[1:], os.environ.get("LEANTOKENS_UPSTREAM"))
    if argv and argv[0] == "stats":
        return cmd_stats()
    if argv and argv[0] == "eval":
        return cmd_eval(argv[1:])
    if argv and argv[0] == "show":
        return cmd_show(argv[1:])
    if argv and argv[0] == "proxy":
        ap = argparse.ArgumentParser(prog="leantokens proxy")
        ap.add_argument("--port", type=int, default=8787)
        ap.add_argument("--upstream", help="send everything to this base URL instead")
        ap.add_argument("--lossy", action="store_true", help="also shorten runs of near-identical log lines")
        ap.add_argument("--smart", action="store_true", help="reference repeated lines, shorten huge outputs")
        ap.add_argument("--cache", action="store_true", help="add Anthropic prompt-caching markers")
        ap.add_argument("--record", metavar="FOLDER", help="save original requests for `leantokens eval`")
        a = ap.parse_args(argv[1:])

        def log(row):
            print("[%s] %s" % (row["p"], _fmt_summary(summarize([row]))), file=sys.stderr, flush=True)

        srv = start_proxy(a.port, a.upstream, log, a.lossy, a.record, a.smart, a.cache)
        url = "http://127.0.0.1:%d" % srv.server_address[1]
        print("leantokens proxy on %s\n  Anthropic: ANTHROPIC_BASE_URL=%s\n  OpenAI:    OPENAI_BASE_URL=%s/v1"
              % (url, url, url), file=sys.stderr, flush=True)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            srv.shutdown()
        return 0
    if argv and argv[0] == "compress":
        argv = argv[1:]

    ap = argparse.ArgumentParser(prog="leantokens", description="Shrink prompts before sending them to an LLM.")
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
