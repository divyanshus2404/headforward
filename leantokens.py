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
import http.client
import http.server
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse

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


def _tabulate(obj):
    """Turn lists of same-shaped objects into {"_cols": [...], "_rows": [[...]]}."""
    if isinstance(obj, dict):
        return {k: _tabulate(v) for k, v in obj.items()}
    if isinstance(obj, list):
        items = [_tabulate(v) for v in obj]
        if len(items) >= 3 and all(isinstance(v, dict) for v in items):
            cols = list(items[0].keys())
            if all(list(v.keys()) == cols for v in items):
                return {"_cols": cols, "_rows": [[v[c] for c in cols] for v in items]}
        return items
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


def compress_tool(text):
    """Compress one tool result (JSON, logs, file reads, command output)."""
    if not isinstance(text, str) or len(text) < MIN_TOOL_CHARS:
        return text
    s = ANSI.sub("", text)
    t = s.strip()
    if t[:1] in "{[":
        try:
            out = json.dumps(_tabulate(json.loads(t)), separators=(",", ":"), ensure_ascii=False)
            return out if len(out) < len(text) else text
        except ValueError:
            pass
    s = LINE_NO.sub(r"\1\2", s)
    lines = _collapse_repeats([l.rstrip() for l in s.split("\n")])
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


def compress_request(body):
    """Compress tool results inside an Anthropic or OpenAI request body, in place.

    Returns (chars_before, chars_after). User and system text is left alone.
    """
    stats = [0, 0]

    def fn(t):
        out = compress_tool(t)
        stats[0] += len(t)
        stats[1] += len(out)
        return out

    for m in body.get("messages") or []:
        if not isinstance(m, dict):
            continue
        if m.get("role") == "tool":  # OpenAI chat completions
            m["content"] = _tool_text(m.get("content"), fn)
        elif isinstance(m.get("content"), list):  # Anthropic
            for block in m["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    block["content"] = _tool_text(block.get("content"), fn)
    items = body.get("input")
    if isinstance(items, list):  # OpenAI Responses API
        for it in items:
            if isinstance(it, dict) and it.get("type") in ("function_call_output", "custom_tool_call_output"):
                it["output"] = _tool_text(it.get("output"), fn)
    return stats[0], stats[1]


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


def _record(provider, before, after):
    try:
        os.makedirs(os.path.dirname(STATS_FILE), exist_ok=True)
        with open(STATS_FILE, "a") as f:
            f.write(json.dumps({"t": int(time.time()), "p": provider, "b": before, "a": after}) + "\n")
    except OSError:
        pass


def make_handler(upstream=None, log=None):
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

            if body and "json" in (self.headers.get("Content-Type") or ""):
                try:
                    data = json.loads(body)
                    before, after = compress_request(data)
                    if before > after:
                        body = json.dumps(data, ensure_ascii=False).encode()
                        _record(provider, before, after)
                        if log:
                            log(provider, before, after)
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
            try:
                while True:
                    chunk = resp.read1(65536)
                    if not chunk:
                        break
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                conn.close()

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _forward

    return Handler


def start_proxy(port=0, upstream=None, log=None):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), make_handler(upstream, log))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


AGENTS = {
    "claude": ("ANTHROPIC_BASE_URL", ""),
    "codex": ("OPENAI_BASE_URL", "/v1"),
    "aider": ("OPENAI_API_BASE", "/v1"),
}


def _fmt(before, after):
    b, a = before // 4, after // 4
    pct = 100 * (b - a) / b if b else 0
    return "~%s -> ~%s tokens of tool output (%.0f%% fewer)" % (format(b, ","), format(a, ","), pct)


def cmd_wrap(agent, args, upstream=None):
    totals = [0, 0]

    def log(p, b, a):
        totals[0] += b
        totals[1] += a

    srv = start_proxy(upstream=upstream, log=log)
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
    if totals[0]:
        print("leantokens: this session %s" % _fmt(*totals), file=sys.stderr)
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
        return
    day = time.time() - 86400
    for label, sel in (("Last 24 hours", [r for r in rows if r["t"] >= day]), ("All time", rows)):
        b, a = sum(r["b"] for r in sel), sum(r["a"] for r in sel)
        print("%-14s %5d requests  %s" % (label, len(sel), _fmt(b, a)))


USAGE = """usage:
  leantokens wrap claude [args...]    run Claude Code through the compressor
  leantokens wrap codex [args...]     run OpenAI Codex CLI (API key auth)
  leantokens wrap aider [args...]     run Aider
  leantokens proxy [--port 8787]      run the proxy for any app or SDK
  leantokens stats                    show tokens saved so far
  leantokens prompt.txt [-l high]     compress a prompt file (- for stdin)
"""


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ("-h", "--help", "help"):
        print(USAGE + __doc__)
        return 0
    if argv and argv[0] == "wrap":
        if len(argv) < 2:
            print(USAGE, file=sys.stderr)
            return 2
        return cmd_wrap(argv[1], argv[2:], os.environ.get("LEANTOKENS_UPSTREAM"))
    if argv and argv[0] == "stats":
        return cmd_stats()
    if argv and argv[0] == "proxy":
        ap = argparse.ArgumentParser(prog="leantokens proxy")
        ap.add_argument("--port", type=int, default=8787)
        ap.add_argument("--upstream", help="send everything to this base URL instead")
        a = ap.parse_args(argv[1:])

        def log(p, b, aft):
            print("[%s] %s" % (p, _fmt(b, aft)), file=sys.stderr, flush=True)

        srv = start_proxy(a.port, a.upstream, log)
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
    ap.add_argument("--model", default="gpt-4o", help="tokenizer model for tiktoken")
    a = ap.parse_args(argv)

    text = sys.stdin.read() if a.file == "-" else open(a.file, encoding="utf-8").read()
    out = compress_tool(text) if a.tool else compress(text, a.level)
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
