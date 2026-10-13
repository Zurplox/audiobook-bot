# audiobook-bot

Epub in → audiobook parts out, fully automatic. Runs on GitHub Actions (private repo).
Telegram is the entire UI: send an `.epub` to the bot, receive `.m4b` parts back.

## Triggers

- **Automatic**: scheduled workflow polls the bot every 30 min for new epubs.
- **Manual**: Actions tab → `audiobook` → Run workflow (optional `minutes` override).
- **Upload**: send the `.epub` file to the bot. Put target minutes in the caption
  (e.g. `90`), or leave empty for the 120-min default. Max 20 MB (Telegram
  bot-download cap). Non-epub files get a one-line reply and are skipped.

## Secrets (repo Settings → Secrets → Actions)

`FISH_API_KEY` `FISH_MODEL` `FISH_VOICE_TS2` `TELEGRAM_BOT_TOKEN`
`TELEGRAM_CHAT_ID` `NIM_API_KEY` `NIM_API_KEY_2` `KILO_API_KEY`
`OPENROUTER_API_KEY`

## Layout

- `pipeline/` — `main.py` orchestrates: poll → extract → vision → summarize →
  synth+deliver loop. Every phase checkpoints to `state/state.json`, which
  persists across runs via the Actions cache. A crashed run resumes mid-book:
  finished sections are skipped, sent parts are never re-sent.
- Length discipline (all rewrite-based, never truncation): sub-800-char spine
  fragments merge at extract (titles kept as subheadings); per-chapter budgets
  with retry-on-overlong; global condense backstop if the total tops 130%.
- `.github/workflows/audiobook.yml` — schedule + manual dispatch.
- `.github/workflows/cleanup.yml` — weekly prune (caches, stray binaries).
  Artifacts self-expire (`retention-days: 7`); Telegram chat is the archive.

## Limits respected

- Telegram bot upload cap 50 MB → parts finalized at ≤40 MB, chapter-aligned.
- Summarize chain: NIM GLM-5.3-flash → Kilo (`kilo-auto/free`) →
  NIM DeepSeek-V4.1-flash → OpenRouter Nemotron-Ultra `:free`.
  Quota errors skip same-key fallbacks; garbage/truncation retries in place.
