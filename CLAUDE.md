# Etsy Agent System

Multi-agent Python system managing Saksham's wife's Etsy shop (CuratedForm).
GitHub: https://github.com/sakshamgulati/etsy-agent-system

## Run

```bash
source venv/bin/activate
python main.py            # scheduler + Telegram webhook (port 5002)
python software_agent.py  # always-on SRE log watcher (separate process)
```

## Test

```bash
python -m pytest tests/ --cov=.   # 206 tests, ~82% coverage
```

## Deploy to Pi

```bash
SSHPASS='Saku@123' sshpass -e rsync -av \
  --exclude='venv/' --exclude='__pycache__/' --exclude='*.pyc' \
  --exclude='.git/' --exclude='logs/*.log' --exclude='db/*.db' \
  -e "ssh -o StrictHostKeyChecking=no -o PubkeyAuthentication=no" \
  /Users/sakshamgulati/etsy/ sakshamgulati@10.0.0.170:/home/sakshamgulati/etsy_agent/
SSHPASS='Saku@123' sshpass -e ssh -o StrictHostKeyChecking=no -o PubkeyAuthentication=no \
  sakshamgulati@10.0.0.170 "sudo systemctl restart etsy-agent"
```

## Key Gotchas

- `DB_PATH` is lazy via `_get_db_path()` in `db/database.py` — never make it module-level (evaluates before `load_dotenv()`)
- Port **5002** (YNAB bot uses 5001 — don't conflict)
- Agents paused via `logs/{agent}.paused` flag files
- Restart via `logs/restart_requested.flag` — systemd picks it up
- `VALID_AGENTS` in `ceo_agent.py` must always match `_agent_map` in `handle_run`
- First run needs `python auth_setup.py` for Etsy OAuth tokens
- Claude model: `claude-haiku-4-5-20251001` (all agents)

## Config

- Telegram bot: @SakEtsy_Bot | Chat ID: 8167084478
- Etsy shop: CuratedForm | API key: in `.env`
- Anthropic key: shared with PersonalFinanceBot
