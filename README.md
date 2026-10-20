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
| Runs of lines already shown earlier, even when an edit renumbered them (`--smart`) | Replaced by `[N lines: same text as lines A-B of tool call X, numbered C-D here]` | Yes, the lines are still in context; `python3 bench/bench_traces.py --verify` checks every reference |
| Routine command output: installs, progress bars, passing tests, build steps (`--smart`) | Runs of 3+ collapse to a count; error lines always kept; full output via `leantokens show ID` | Recoverable on demand |
| Outputs over 400 lines (`--smart`) | Start, end and error lines kept; the full text is saved locally and the agent can read any part with `leantokens show ID --lines A-B` | Recoverable on demand |
| Runs of near-identical log lines (`--lossy` only) | First and last line kept, middle replaced by `... [N similar lines omitted]` | No |
| Your messages and system prompt | Untouched | n/a |

Compression is deterministic and never changes earlier turns, so Anthropic and OpenAI prompt caching keeps hitting.

### Measured on real agent sessions

200 public SWE-agent sessions (5,463 API requests) where an agent fixes real GitHub issues by opening files, searching, editing and running tests. Counted with `tiktoken`; reproduce with `python3 bench/bench_traces.py`.

Two independent samples of 200 sessions each (dataset offsets 0 and 5000):

| Mode | Final context | Total session input | Median session |
|---|---|---|---|
| leantokens | 13.9% / 14.9% smaller | 10.8% / 13.2% smaller | 2.7% / 0.0% |
| `--smart` | **30.5% / 35.7% smaller** | **21.4% / 27.4% smaller** | **22.4% / 25.6%** |
| `--smart --lossy` | 30.9% / 35.8% smaller | 21.7% / 27.4% smaller | 23.0% / 25.6% |

Most of the gain comes from agents re-opening files they have already seen: 65% of tool-output tokens in these sessions are file views, and half of all tool-output lines repeat lines shown earlier. JSON-heavy and log-heavy work (API calls, CI output) saves much more; in a synthetic session of that kind, default mode saved 60%.

`--smart` keeps every line the model saw available in context or on disk, but it changes how the agent sees repeated content. Until you have run `leantokens eval` on your own work, treat it as something to measure, not a guaranteed same-result setting.

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
leantokens wrap --smart claude    # biggest savings: references repeated lines, shortens huge outputs
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

API keys pass straight through to the provider and are never stored. Add `--cache` to have the proxy add Anthropic prompt-caching markers for apps that do not set any. Repeated context then costs about 10% of the normal input price, but a request that is never repeated costs 25% more, so use it for chat apps and agents, not one-off calls. If `ANTHROPIC_BASE_URL` or `OPENAI_BASE_URL` is already set when the proxy starts, it forwards there instead of the public API, so company gateways keep working.

### Claude chat (claude.ai and the desktop app)

The chat apps talk only to Anthropic's servers, so nothing can sit in between. Instead, compress what you paste:

```bash
leantokens clip                       # compresses whatever you copied; then paste into Claude
leantokens pack src/ logs/app.log     # bundles files into one compact <documents> block on your clipboard
leantokens pack . --max-tokens 50000  # stop adding files at a token budget
leantokens count notes.md             # Claude tokens before and after (exact with ANTHROPIC_API_KEY, free)
```

`clip` uses the JSON and log rules on data and the prompt rules on prose: copying a 30-row API response went from 1,112 to 90 tokens, and a chatty question from 31 to 11. `pack` follows `.gitignore`, skips lockfiles, binaries, `node_modules` and anything that looks like a secret (`.env`, keys, credentials), trims trailing spaces in code and compresses JSON and logs. It uses the `<document path="...">` format Anthropic recommends for long inputs.

### Fix the habits that waste tokens in Claude Code

```bash
leantokens audit                 # what wastes tokens in this project
leantokens tune claude           # preview token-saving Claude Code settings
leantokens tune claude --apply   # save them to .claude/settings.local.json (backup kept)
```

`audit` reports how many tokens your `CLAUDE.md` files cost on every request, lockfiles and build output Claude can open, whether command output is capped, and single files big enough to be expensive to read. `tune` adds `Read` deny rules for lockfiles, build folders and minified or source-map files, and caps each command's output with `BASH_MAX_OUTPUT_LENGTH`. Existing settings are kept, and nothing is written without `--apply`. Restart Claude Code afterwards.

Habits that save the most, with or without leantokens: `/clear` between unrelated tasks, `/compact` in long ones, name the exact file and function, ask for short answers, and use a smaller model with `/model` for simple edits.

### See what Claude Code actually used

```bash
leantokens usage                 # last 7 days, by day
leantokens usage --by model      # or --by project, --days 30
```

Reads Claude Code's own logs in `~/.claude/projects` (nothing is sent anywhere) and shows replies, input, cache writes, cache reads and output, plus how much input came from the prompt cache.

### Give Claude a map instead of whole files

```bash
leantokens map --copy            # every file's classes and functions, with line numbers
leantokens pack src/ --outline   # signatures only for code, full text for everything else
```

`map` describes the 85 KB leantokens source in about 1,400 tokens. Paste it, or put it in `CLAUDE.md`, so Claude can go straight to the right function. `pack --outline` halved the token cost of packing this repository (72,758 to 35,026), and `pack` now lists the most expensive files.

### Always-on proxy for terminal Claude Code (macOS)

Instead of typing `wrap` each time, run the proxy in the background and point a project (or every project) at it:

```bash
leantokens desktop on ~/code/my-app   # one project first
leantokens desktop status             # running? which settings use it?
leantokens desktop off                # stop it and undo every settings change
```

The proxy runs as a login item (launchd), restarts within about 2 seconds if it stops, and answers a health check at `http://127.0.0.1:8787/leantokens/health`. `on` edits settings only after the proxy is confirmed running, never replaces an `ANTHROPIC_BASE_URL` you set yourself, and keeps a backup.

**Not for the Claude desktop app.** Tested: Code sessions in the desktop app set their own connection to Anthropic and ignore `ANTHROPIC_BASE_URL` in settings, so their requests never reach the proxy. This works for Claude Code started in a terminal.

### Check your setup

```bash
leantokens doctor
```

Checks Python, the `leantokens` command, Claude Code and its sign-in, `ANTHROPIC_BASE_URL`, that the proxy can reach Anthropic, and the stats file, and prints the fix for anything wrong.

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
