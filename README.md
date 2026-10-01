# Claude Code Status Limits

A status bar script for [Claude Code](https://claude.ai/code) that visualizes context window usage and rate limit consumption in real time.

![Preview](assets/preview.png)

[Русская версия](README.ru.md)

## What it shows

### Context bar

Shows how full the current conversation context window is. Claude Code reserves a fixed 33k-token autocompact buffer and automatically compresses the conversation history when that threshold is reached. The buffer percentage depends on the context window size: ~16.5% for 200k, ~3.3% for 1M.

The bar scales to the usable portion of the window, so it reaches 100% exactly when autocompact is about to trigger.

Its width follows the window size — one character per 50k tokens, never fewer than six. A 1M window gets 20 characters, 500k gets 10, and anything up to 300k gets the six-character minimum. The point is that a given number of tokens always occupies the same width, whatever model you are on.

The fill color escalates in three steps:

**Gray** — normal.

**Amber** — the context passed 150k tokens, the boundary Claude Code itself singles out when it reports usage, or it has entered the autocompact buffer.

**Red** — the context passed 300k tokens. On a large window autocompact fires so late that the context can grow well past a reasonable size unnoticed.

The empty part of the bar is tinted by zone — neutral below 150k, faintly warm between 150k and 300k, faintly red above — so the distance to the next threshold is readable before it is crossed.

### Rate limit bars

Each bar represents a usage quota within a fixed time window — either 5 hours or 7 days. The bar label and current percentage are shown as text inside the bar.

#### Reading the colors

Every rate limit bar encodes two things at once: how much of the quota has been consumed, and how far through the reset window you are. The color tells you which of the two is ahead of the other:

**Bright green** — consumption and time are both progressing normally. Nothing to worry about.

**Dark green** — you have used little of the quota relative to how much time has passed. You are well within the limit and have room to spare.

**Amber** — you are consuming the quota faster than time is passing. At this pace you are likely to hit the limit before the window resets.

**Dark gray** — no data yet, or the window just opened.

#### Wait time

An amber bar tells you that the quota is being consumed faster than time is passing, but not by how much. Therefore, its label shows how long you need to refrain from spending quota so that time catches up with consumption and the bar returns to the green zone: `7d (wait 21h)`. Usually this is much sooner than the window reset; at 100% consumption the wait time equals the reset time. The time is written with a single leading unit — `45m`, `2h`, `3d` — so it fits even a narrow segment.

### Session badges

A row of badges after the bars describes the session itself:

- **Model** — the display name, with a ⚡ when fast mode is on.
- **Context window** — its size as a number, `200k` or `1m`.
- **Effort** — a short label for the thinking effort level: `L`, `M`, `H`, `xH` and `Mx`, for low, medium, high, xhigh and max.

Model and effort share one four-step color scale — slate, green, amber, red — so the same level of intensity looks the same on both. Green, amber, and red are the same shades as on the bars. The window size badge stays neutral, dark gray like the empty part of the bar: it is a number, not a category.

Each of the three is also compared against the defaults. If one differs, its badge is marked in the padding cells and a red badge **DEF** follows the row. The usual cause is a resumed session: it keeps the model recorded in its transcript and ignores a later change of default.

- **Model** is compared down to the version: Opus 4.5 with a default of Opus 5.5 counts as different. The reference is the `model` field in `~/.claude/settings.json`, then the `ANTHROPIC_DEFAULT_MODEL` environment variable. If the model is set to "Default" and the field is not saved, the reference comes from the Claude Code model catalog cache (`~/.claude/cache/model-catalog`). An alias like `opus` is resolved to the current family version from the same catalog. The catalog format is internal and may change; without it the model check is skipped.
- **Context window** is checked only if the `model` field explicitly includes a size, like `claude-opus-5-5[1m]`.
- **Effort** is compared against `modelSettings.<model>.effortLevel` — where `/effort` writes it — and if not set there, against the top-level `effortLevel`.

A red badge **API** means the last usage API refresh failed and the per-model bars are missing. Without it a broken sync is indistinguishable from simply having no extra quotas.

### Project folder

The project directory name is appended at the very end, so several open windows can be told apart at a glance. It is taken from the directory Claude Code was started in, which keeps it stable for the life of the window even when the session moves into a subdirectory.

## Data sources

The script receives data from two independent sources.

**Claude Code itself** passes context window fill and the 5h/7d rate limit state after each turn. These values are only available once the conversation has started, so the very first render of a new chat relies on cached data from the previous session.

**The Claude.ai usage API** provides per-model quotas such as Sonnet 7d and Opus 7d, including limits scoped to a single model — the weekly Fable one, for example. It is queried using the OAuth token that Claude Code itself uses to authenticate, so no separate credentials are needed. The API is refreshed at most once every 5 minutes, or once a minute after a failed attempt, since those failures are usually transient.

## Caching

The last known state of every bar is saved to `~/.claude/status_limits_cache.json`. This means bars always show something meaningful even at the start of a fresh chat, before any turn data has come in. As new data arrives — from Claude Code or from the API — the cache is updated, and the two sources are merged: when they overlap, the live Claude Code data takes priority over the API.

Every open Claude Code window shares this one file, so the script takes a lock around each read-modify-write cycle and replaces the file atomically.

## Setup

Add the following to `~/.claude/settings.json`:

```json
"statusLine": {
  "type": "command",
  "command": "python /path/to/claude-code-status-limits/status_limits.py"
}
```

Adjust the path to wherever you cloned the repository. Python 3.10+ and `curl` are required.

### Bar width

Claude Code places other content (such as a token counter) to the right of the status bar output, so the bars never occupy the full terminal width. By default the script renders at 120 columns. To control how wide the strip is, pass the desired width as an argument:

```json
"command": "python /path/to/claude-code-status-limits/status_limits.py 100"
```

Adjust the value until the bars fit the available space in your terminal.

The width covers the bars and the badge row. The folder name is deliberately left out of it and written past the edge, where Claude Code trims it at the real terminal width — so a long project name never takes space away from the bars.
