# Etsy Automation

AI-powered Etsy shop management system. Automates listing optimization, pricing, ad budget management, social media cross-posting, and financial reporting via Claude AI agents.

## Features

- Etsy API v3 integration (listings, orders, ads, analytics)
- Claude AI agents for pricing, copy, and budget decisions
- Social media cross-posting (Pinterest, Instagram)
- Telegram notifications and approval flows
- Profit/loss tracking with configurable COGS

## Setup

### 1. Clone and install dependencies

```bash
cd etsy
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.template .env
# Edit .env and fill in all required secrets
```

### 3. Obtain API credentials

- **Etsy:** Create an app at https://www.etsy.com/developers/your-apps, complete OAuth2 flow for access/refresh tokens.
- **Anthropic:** https://console.anthropic.com/
- **Telegram:** Create a bot via @BotFather, get chat ID via @userinfobot.
- **Pinterest:** https://developers.pinterest.com/
- **Instagram:** Meta Graph API via https://developers.facebook.com/

## Project Structure

```
etsy/
├── agents/          # Claude AI agent logic
├── integrations/    # API clients (Etsy, Pinterest, Instagram, Telegram)
├── db/              # Database models and helpers
├── logs/            # Runtime logs (gitignored)
├── prompts/         # LLM prompt templates
├── requirements.txt
└── .env.template
```

## Running

```bash
source venv/bin/activate
python main.py
```
