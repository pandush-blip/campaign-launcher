# Campaign Launcher

Turn a lead list and a copy file into ready-to-review cold email campaign drafts: correctly named, correctly configured, copy loaded, leads loaded, with a full QA report on every segment.

Built and used daily by [The Revenue Inbox](https://therevenueinbox.com). Works with **Instantly** and **OutFalcon** workspaces.

**Draft-first by design.** The tool prepares and verifies. Nothing starts sending without a human and a typed confirmation.

## Why this exists

Setting up campaigns by hand takes about 40 minutes each: duplicate the settings campaign, rebuild the sequence, paste copy, split the list, upload leads, check every variable. Multiply that by one campaign per audience segment.

This tool turns a launch folder into draft campaigns in minutes, and it catches the mistakes before they ship: broken spintax, unknown variables, empty merge fields, mismatched lead counts. If it is not in the form, it does not exist.

## What it does

- **Duplicates your settings campaign** per segment, so every campaign inherits the same schedule, accounts, limits, and options.
- **Parses hand-written copy files** (steps, variants, spintax, delays) into the sequencer's sequence format.
- **Runs the QA gate** before anything is created: spintax syntax, variable coverage per lead row, rendered-sample checks, lint rules (em dashes banned, word count, link count), and an optional AI proofread. Each segment gets a PASS / WARN / BLOCK badge in `qa_report.html`.
- **Splits and dedupes leads** across segments (priority rules, suppression, invalid-email drops, cross-segment dedupe) and reports every removal.
- **Creates drafts and reads them back**: name, status, settings, sequence, and lead counts are verified from the API, not from the create call.
- **Resumes safely**: interrupted runs pick up from state without duplicating campaigns or leads.

## Quickstart

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,web]"
cp .env.example .env        # fill in your API keys
tri-launch --help
```

## Commands

| Command | What it does |
|---|---|
| `tri-launch new <client> <batch>` | Scaffold a launch folder with templates |
| `tri-launch doctor <dir>` | Keys, settings campaign, accounts (no writes) |
| `tri-launch qa <dir>` | Stages 1-3: parse, QA, write `qa_report.html` (no writes) |
| `tri-launch plan <dir>` | Print the creation plan (no writes) |
| `tri-launch create <dir>` | Full run: duplicate, copy, variables, leads, readback |
| `tri-launch resume <dir>` | Continue an interrupted run from state |
| `tri-launch verify <dir>` | Re-run readback checks on created campaigns |
| `tri-launch campaigns [--search TEXT]` | List campaigns (read-only) to pick a settings source |
| `tri-launch cleanup <dir> --confirm-name "<exact name>"` | Delete tool-created campaigns only |
| `tri-launch web` | The web UI (FastAPI), same pipeline from a browser |

## The launch folder

```text
launches/<client>/<date>-<batch>/
  launch.yaml          # client, settings campaign, naming, segments, lint rules
  leads/<segment>.csv  # or one file with a segment column (split_column mode)
  copy/<segment>.md    # one copy file per segment
```

## The copy file format

Plain text. Steps, variants, spintax, fallbacks:

```text
# Step 1
delay: 0

## Variant A
Subject: {{RANDOM | quick question | idea for {{companyName}}}}
Body:
{{RANDOM | Hi | Hey}} {{firstName | there}},

First paragraph...

{{sendingAccountFirstName}}

# Step 2
delay: 3

## Variant A
Subject:
Body:
...
```

`delay` = days before the next step. An empty subject means "reply in the same thread". Bodies are converted to editor-style `<div>` HTML on write. Variables must be built-ins (firstName, lastName, companyName, website, phone, personalization, email, jobTitle, sendingAccountFirstName, accountSignature) or columns in that segment's CSV. Missing unsubscribe language is filled with the house default block (override per folder via `unsubscribe.mode: require|off`).

One campaign per list value (no segment list needed):

```yaml
segmentation:
  mode: list_name
  column: "List Name"
  source: leads/all_leads.csv
  shared_copy: copy/default.md
  exclude: ["DND*", "*DO NOT DISTURB*"]   # never uploaded
  min_leads: 2
```

## Draft-first, human-launched

- A hard endpoint allowlist in the HTTP client: the tool cannot call anything that starts sending, except the one explicit launch path.
- Launching is a separate step, in the web app or CLI: it requires a readiness readback (draft status, lead count, daily limit, sending accounts) and typing the exact confirmation phrase (for example `LAUNCH 10`). Only campaigns this tool created for the session are activated, and every activation is verified by readback.
- `Pause all campaigns` stops every active campaign in the batch.
- Secrets stay in `.env`. Never logged, never committed. Every created ID goes to local `state/`; updates and deletes are limited to those IDs.

## Tests

```bash
pytest
```

## Layout

```text
src/tri_launch/    the engine (CLI, QA, spintax, leads, create, readback, web UI)
tests/             the test suite
templates/         launch folder starters (launch.yaml, copy file)
```

---

Built by [The Revenue Inbox](https://therevenueinbox.com). Steal it, self-host it, change it.
