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
| JSON lists of same-shaped objects | Keys written once, values as rows; columns with one value for every row are stated once (`{"_cols", "_rows", "_same"}`) | Yes, `expand()` rebuilds the original exactly |
| The same tool output seen again in a conversation | Replaced by `[Same output as tool call X above.]` | Yes, the earlier copy is still in context |
| Logs and command output | Repeated lines collapse to one line with `[xN]`, color codes and trailing spaces removed | Yes, apart from color codes |
| File reads | Line-number padding trimmed | Yes |
| Runs of near-identical log lines (`--lossy` only) | First and last line kept, middle replaced by `... [N similar lines omitted]` | No |
| Your messages and system prompt | Untouched | n/a |

Compression is deterministic and never changes earlier turns, so Anthropic and OpenAI prompt caching keeps hitting.

### Measured

A simulated agent session (a 150-line file read twice, a 60-row API result twice, 120 lines of worker logs, a test run twice), counted with `tiktoken`:

| | Tokens | Saved |
|---|---|---|
| Original | 18,466 | |
| leantokens | 7,304 | 60% |
| leantokens `--lossy` | 3,987 | 78% |

Your savings depend on the session. JSON-heavy, log-heavy and repetitive work saves the most. Sessions that mostly read new source code save little, because code is left as written.

## Check it on your own work

`leantokens stats` reports what the provider actually billed (input, cache hits, output) and the tokens saved, converted at your real billed rate.

To check that answers don't change, record real requests and replay them with and without compression:

```bash
leantokens wrap --record ~/lt-rec claude     # work as usual; requests are saved without API keys
leantokens eval ~/lt-rec --tokens-only       # exact token savings (free on Anthropic, uses count_tokens)
leantokens eval ~/lt-rec                     # also asks the model both ways and compares the answers
```

`eval` needs `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`, and the full run costs about two API calls per recording. Recordings contain your conversation, so they stay on your machine; delete the folder when done.

## Usage

```bash
leantokens wrap claude            # Claude Code
leantokens wrap --lossy claude    # also shorten near-identical log lines
leantokens wrap codex             # Codex CLI (API key login)
leantokens wrap aider             # Aider
leantokens stats                  # billed tokens, cache hits, tokens saved
leantokens eval ~/lt-rec          # replay recordings, compare savings and answers
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
