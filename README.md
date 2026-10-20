# leantokens

[![PyPI](https://img.shields.io/pypi/v/leantokens)](https://pypi.org/project/leantokens/)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue)](https://pypi.org/project/leantokens/)
[![Tests](https://github.com/divyanshus2404/leantokens/actions/workflows/test.yml/badge.svg)](https://github.com/divyanshus2404/leantokens/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

**Cut the tokens Claude Code spends re-reading tool output, without hiding what it needs.**

Coding agents spend most of their tokens on tool output: test runs, build logs, `git log`, file reads, JSON from APIs. leantokens shrinks that output before the model sees it. Error lines are always kept, and anything it shortens is saved so the agent can read it back.

One Python file. No dependencies. Works on macOS, Linux and Windows.

```bash
pip install leantokens
leantokens hooks install global   # every Claude Code session, desktop app included
leantokens doctor                 # checks your setup and says what to fix
```

Start a new Claude Code session and work as usual. `leantokens stats` shows what you saved.

## What it does

A pytest run with 42 tests and one failure, **601 tokens → 143**:

```text
============================= test session starts ==============================
collected 42 items

[40 passing test lines omitted; full output: leantokens show 256d7a092127]
tests/test_orders.py::test_refund_total FAILED
tests/test_orders.py::test_missing_sku PASSED

=================================== FAILURES ===================================
______________________________ test_refund_total _______________________________
    def test_refund_total():
>       assert round(19.99 * 3, 2) == 59.98
E       assert 59.97 == 59.98
tests/test_orders.py:88: AssertionError
========================= 1 failed, 41 passed in 0.42s =========================
```

The failure, its assertion and the summary are untouched. The 40 passing lines are one line, and Claude can run `leantokens show 256d7a092127` to see them.

## Compared with rtk and Headroom

The same 7 real command outputs from one Mac, run through each tool. "Facts kept" counts strings each output must still contain: failing test names and values, error lines, list items.

| Output | Raw tokens | rtk | Headroom | leantokens |
|---|---|---|---|---|
| Unit tests, 2 failures | 1,899 | 67, failures hidden | 1,899 | 293 |
| `git log --stat`, 40 commits | 4,869 | 1,271 | 4,869 | 3,800 |
| `grep`, 1 file | 963 | 258, matches cut | 963 | 963 |
| `grep` across 3 paths | 4,921 | 1,741 | 3,150 | 3,385 |
| `ls -la`, large folder | 10,445 | 3,077 | 10,445 | 7,264 |
| System log, 600 lines | 37,040 | 614, error cut | 36,279 | 21,070 |
| JSON, 25 items | 2,635 | 93, 1 of 25 shown | 1,592 | 1,388 |
| **Tokens saved** | | **89%** | **6%** | **39%** |
| **Facts kept** | | 9 of 19 | 19 of 19 | 19 of 19 |

rtk 0.43.0 summarizes hardest and saves its full output to a file the agent can read later. Headroom 0.41.0 was tested with its default `compress()`; its proxy has more options. This is a small benchmark and I wrote leantokens, so run it yourself: `python3 bench/compare_tools.py`.

## Ways to run it

| | Hooks | Always-on proxy | Wrap |
|---|---|---|---|
| **Command** | `leantokens hooks install global` | `leantokens desktop on global` | `leantokens wrap claude` |
| **What it compresses** | Output of noisy commands | Every tool result: JSON, repeated reads, logs | Every tool result, one session |
| **Claude Code in a terminal** | Yes | Yes (macOS) | Yes |
| **Claude Code in the desktop app** | Yes | No, the app ignores base URLs | No |
| **Codex CLI, Aider** | No | Set the URL yourself | Yes |
| **Undo** | `leantokens hooks uninstall global` | `leantokens desktop off` | Quit the agent |

Hooks and the proxy work together: hooks shrink command output as it's produced, and the proxy compresses everything else.

### Hooks

```bash
leantokens hooks install global          # or a single project: leantokens hooks install ~/code/my-app
leantokens hooks uninstall global
```

A Claude Code `PreToolUse` hook pipes these commands through the filter: tests (`pytest`, `npm test`, `go test`, `cargo test` and similar), installs and builds (`pip install`, `npm install`, `make`, `cargo build`, `docker build`), plus `git log`, `ls -l`, `grep`, `rg` and `log show`. Every other command is left alone.

- Colour codes and progress-bar redraws are removed.
- Runs of routine lines (passing tests, download lines, build steps) collapse to a count.
- Git logs, long listings, multi-file grep results and timestamped logs are compacted: full hashes shortened, a single author or owner stated once, stat bar graphs dropped, grep matches grouped under their file, near-identical log lines counted on the first one.
- Error-like lines are always kept, and the command's exit code is preserved.

### Always-on proxy (macOS)

```bash
leantokens desktop on global      # or one project first: leantokens desktop on ~/code/my-app
leantokens desktop status         # running? which settings use it? where does it forward?
leantokens desktop off            # stop it and undo every settings change
```

Runs the proxy at login (launchd), restarts it within about 2 seconds if it stops, and points Claude Code's settings at it. Settings change only after the proxy is confirmed running, and a backup is kept.

**Already using a gateway?** If your Claude Code settings point `ANTHROPIC_BASE_URL` at a router, cost tracker or company proxy, leantokens goes in front of it: Claude Code → leantokens → your gateway → Anthropic. `desktop off` puts your URL back.

### Wrap one session

```bash
leantokens wrap claude            # Claude Code
leantokens wrap --smart claude    # biggest savings, see below
leantokens wrap --lossy claude    # also shorten near-identical log lines
leantokens wrap codex             # Codex CLI (API key login)
leantokens wrap aider             # Aider
```

Starts a private proxy, launches the agent pointed at it, and prints what you saved when you quit.

### Proxy for your own app or SDK

```bash
leantokens proxy --port 8787
export ANTHROPIC_BASE_URL=http://127.0.0.1:8787
export OPENAI_BASE_URL=http://127.0.0.1:8787/v1
```

API keys pass straight through to the provider and are never stored. If `ANTHROPIC_BASE_URL` or `OPENAI_BASE_URL` is already set when the proxy starts, it forwards there, so company gateways keep working. Add `--cache` to add Anthropic prompt-caching markers for apps that set none: repeated context then costs about 10% of normal input, but a request that is never repeated costs 25% more, so use it for chat apps and agents, not one-off calls.

## What gets compressed

| Input | What happens | Lossless? |
|---|---|---|
| JSON lists of same-shaped objects | Keys written once, values as rows; columns with one value for every row are stated once | Yes, `expand()` rebuilds the original exactly |
| The same tool output seen again | Replaced by `[Same output as tool call X above.]` | Yes, the earlier copy is still in context |
| Logs and command output | Repeated lines collapse to one line with `[xN]`; colour codes and trailing spaces removed | Yes, apart from colour codes |
| File reads | Line-number padding trimmed | Yes |
| Routine command output (hooks, `--smart`) | Runs of 3+ installs, progress bars, passing tests or build steps collapse to a count | Recoverable with `leantokens show ID` |
| Git log, `ls -l`, grep, timestamped logs (hooks, `--lossy`) | Repeated and derivable parts removed; every distinct line kept | Recoverable with `leantokens show ID` |
| Runs of lines already shown earlier, even after an edit renumbered them (`--smart`) | Replaced by a reference to the earlier lines | Yes, the lines are still in context |
| Outputs over 400 lines (`--smart`) | Start, end and error lines kept | Recoverable with `leantokens show ID --lines A-B` |
| Runs of near-identical log lines (`--lossy`) | First and last kept, middle replaced by a count | No |
| Your messages and the system prompt | Untouched | n/a |

Compression is deterministic and never changes earlier turns, so Anthropic and OpenAI prompt caching keeps hitting.

## Measured on real agent sessions

400 public SWE-agent sessions, in two independent samples of 200, where an agent fixes real GitHub issues by opening files, searching, editing and running tests. Counted with `tiktoken`; reproduce with `python3 bench/bench_traces.py`.

| Mode | Final context | Total session input | Median session |
|---|---|---|---|
| default | 13.9% / 14.9% smaller | 10.8% / 13.2% smaller | 2.7% / 0.0% |
| `--smart` | **30.5% / 35.7% smaller** | **21.4% / 27.4% smaller** | **22.4% / 25.6%** |
| `--smart --lossy` | 31.2% / 35.9% smaller | 22.1% / 27.6% smaller | 22.7% / 26.0% |

Most of the `--smart` gain comes from agents re-opening files they have already seen: 65% of tool-output tokens in these sessions are file views, and half of all tool-output lines repeat lines shown earlier. `--smart` keeps every line available in context or on disk, but it changes how the agent sees repeated content, so measure it on your own work before relying on it.

## Check it on your own work

`leantokens stats` reports what the provider actually billed (input, cache hits, output) and the tokens saved, at your real billed rate.

To check that answers don't change, record real requests and replay them with and without compression:

```bash
leantokens wrap --record ~/lt-rec claude     # work as usual; requests are saved without API keys
leantokens eval ~/lt-rec --tokens-only       # exact token savings (free on Anthropic, uses count_tokens)
leantokens eval ~/lt-rec                     # also asks the model both ways and compares the answers
```

`eval` needs `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` and costs about two API calls per recording. Recordings contain your conversation, so they stay on your machine; delete the folder when done.

## More tools

### Fix the habits that waste tokens

```bash
leantokens audit                 # what wastes tokens in this project
leantokens tune claude --apply   # add token-saving Claude Code settings (preview without --apply)
leantokens usage --by model      # Claude Code token use from its own local logs (--by day|project, --days N)
```

`audit` reports what your `CLAUDE.md` files cost on every request, lockfiles and build output Claude can open, whether command output is capped, and files big enough to be expensive to read. `tune` adds `Read` deny rules for lockfiles, build folders and minified or source-map files and caps command output at 15,000 characters; existing settings are kept and a backup is made.

Habits that save the most, with or without leantokens: `/clear` between unrelated tasks, `/compact` in long ones, name the exact file and function, and use a smaller model with `/model` for simple edits.

### Give Claude a map instead of whole files

```bash
leantokens map --copy            # every file's classes and functions, with line numbers
leantokens pack src/ --outline   # signatures only for code, full text for everything else
```

`map` describes the leantokens source in about 1,400 tokens. Put it in `CLAUDE.md` or paste it so Claude goes straight to the right function.

### Claude chat (claude.ai and the desktop app)

Chat apps talk only to Anthropic's servers, so nothing can sit in between. Compress what you paste instead:

```bash
leantokens clip                       # compress whatever you copied, then paste into Claude
leantokens pack src/ logs/app.log     # bundle files into one compact <documents> block on your clipboard
leantokens count notes.md             # Claude tokens before and after
pbpaste | leantokens - -l high        # compress a prompt (levels: light, medium, high, max)
```

`pack` follows `.gitignore` and skips lockfiles, binaries, `node_modules` and anything that looks like a secret (`.env`, keys, credentials).

## Limits

- The proxy compresses only tool output. Your messages and the system prompt are untouched.
- Savings depend on the work. Log-heavy, test-heavy and JSON-heavy sessions save the most; reading source code saves little, because code is left as written.
- Conversation history usually costs more than any tool output. `/clear` and `/compact` matter.
- The Claude desktop app ignores `ANTHROPIC_BASE_URL`, so use hooks there.
- An `ANTHROPIC_BASE_URL` exported in your shell overrides Claude Code settings and bypasses the always-on proxy. `leantokens doctor` warns about this.
- Codex signed in with a ChatGPT account talks to ChatGPT's servers and bypasses the proxy. Use an API key login.

## No pip?

leantokens is one file with no dependencies. Download [`leantokens.py`](leantokens.py) and run `python3 leantokens.py` wherever this page says `leantokens`. For exact token counts, install `tiktoken` or `pip install "leantokens[exact]"`.

## License

MIT. Issues and pull requests are welcome, especially examples where leantokens hid something your agent needed.
