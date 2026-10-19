# leantokens

Cut the tokens your AI coding agent spends.

Coding agents spend most of their tokens re-reading tool output: JSON from APIs, logs and file reads. leantokens runs a small local proxy between the agent and the model and shrinks that output before it is sent. You keep using Claude Code or Codex as before.

```bash
pip install leantokens
leantokens wrap claude
```

## What it does

| Input | What happens | Lossless? |
|---|---|---|
| JSON lists of same-shaped objects | Keys written once, values as rows (`{"_cols": [...], "_rows": [...]}`) | Yes |
| Logs and command output | Repeated lines collapse to one line with `[xN]`, color codes and trailing spaces removed | Yes, apart from color codes |
| File reads | Line-number padding trimmed | Yes |
| Your messages and system prompt | Untouched | n/a |

Compression is deterministic: the same tool output always becomes the same bytes, so Anthropic and OpenAI prompt caching still hits on earlier turns.

In a test request with a 40-row JSON result and a 31-line log, tool output went from about 1,164 to 329 tokens (72% fewer). Savings depend on the session. JSON-heavy and log-heavy work saves the most, and sessions that mostly read source code save little.

## Usage

```bash
leantokens wrap claude            # Claude Code
leantokens wrap codex             # Codex CLI (API key login)
leantokens wrap aider             # Aider
leantokens stats                  # tokens saved so far
```

### Proxy for your own app or SDK

```bash
leantokens proxy --port 8787
export ANTHROPIC_BASE_URL=http://127.0.0.1:8787
export OPENAI_BASE_URL=http://127.0.0.1:8787/v1
```

API keys pass straight through to the provider and are never stored. If `ANTHROPIC_BASE_URL` or `OPENAI_BASE_URL` is already set when the proxy starts, it forwards there instead of the public API, so company gateways keep working.

### Compress a prompt

For pasting into chat apps:

```bash
pbpaste | leantokens - -l high | pbcopy     # macOS clipboard in, clipboard out
leantokens prompt.txt -l high               # levels: light, medium, high, max
leantokens api.json --tool                  # tool-output rules on a file
```

Prompt levels cut whitespace, filler words, greetings, sign-offs, repeated sentences, hedges and articles. Code blocks are never rewritten, except that `high` minifies JSON blocks and `max` drops comment-only lines.

Install `tiktoken` for exact token counts: `pip install "leantokens[exact]"`.

## Limits

- Only tool output is compressed when proxying.
- Codex signed in with a ChatGPT account talks to ChatGPT's servers, so it bypasses the proxy. Use an API key login.
- Browser chat apps (claude.ai, chatgpt.com) can't be proxied. Use the prompt compressor.

## No pip?

leantokens is one file with no dependencies. Download `leantokens.py` and run `python3 leantokens.py wrap claude`.

## License

MIT
