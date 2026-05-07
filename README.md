# bloom_india

Local-first, open-source **point-in-time financial data platform** for Indian markets (NSE/BSE).

No cloud. No API keys. No paid data. Everything runs on your machine.

---

## What it does

- Downloads and caches NSE/BSE data locally (prices, XBRL filings, corporate actions)
- Provides a **point-in-time safe API** — every data point is tagged with when the market actually knew it, preventing look-ahead bias in backtests
- Bloomberg-style **Streamlit terminal** for fundamental analysis
- Clean Python API for building quantitative strategies

## Data sources

| Data | Source | History |
|------|--------|---------|
| EOD prices (OHLCV) | NSE bhavcopy archive | 2010 → present |
| Quarterly financials | NSE XBRL filings | 2018 → present |
| Announcement dates | BSE corporate announcements | 2014 → present |
| Corporate actions | NSE corporate actions | 2010 → present |
| Index constituents | NSE index archive | 2010 → present (PIT) |

All data is sourced from official NSE/BSE public archives — no scraping of private APIs.

## Installation

```bash
git clone https://github.com/debjit99/bloom_india.git
cd bloom_india
pip install -r requirements.txt

# First-time setup — sets your data directory
python -m bloom_india.setup
```

Or install as a package:

```bash
pip install git+https://github.com/debjit99/bloom_india.git

# Then run setup
bloom-india-setup
```

## Quick start

```python
from bloom_india.api import get_fundamentals, get_ohlcv, get_universe

# Point-in-time fundamental snapshot — only data that was public on this date
df = get_fundamentals("2024-01-25")
print(df[["symbol", "quarter_label", "revenue", "pat", "sue"]].head())

# Price time series
prices = get_ohlcv("HDFCBANK", from_date="2023-01-01", to_date="2024-12-31")
print(prices.tail())

# PIT index constituents
nifty50 = get_universe("NIFTY 50", date="2023-06-15")
print(nifty50)
```

## Build databases

```bash
# Download and build price database (~10GB, takes 30-60 min first time)
python -m bloom_india.scripts.build_price_db

# Download and build fundamental database
python -m bloom_india.scripts.build_fundamental_db

# Daily update (run as cron job)
python -m bloom_india.scripts.update_daily
```

## Launch terminal

```bash
streamlit run bloom_india/terminal/app.py
```

Opens at `http://localhost:8501`

## Configuration

All paths are set in `config.yaml` (created by setup, never committed to git):

```yaml
storage:
  base_dir: "~/bloom_india_data"   # all data lives here
```

Override any individual path:

```yaml
storage:
  base_dir: "~/bloom_india_data"
  fundamental_db: "/fast_ssd/fundamentals.parquet"   # override one path
```

Or use an environment variable:

```bash
export BLOOM_INDIA_CONFIG=/path/to/my/config.yaml
```

## Project structure

```
bloom_india/
├── config.yaml.example    ← template (copy to config.yaml and edit)
├── config.py              ← loads config, exposes CONFIG and get_path()
├── setup.py               ← interactive setup wizard
│
├── data/
│   ├── fetch/             ← raw data downloaders
│   ├── process/           ← cleaners and transformers
│   └── api/               ← public Python API
│
├── backtest/              ← signal generation, portfolio construction, analytics
├── terminal/              ← Streamlit Bloomberg-style UI
└── scripts/               ← one-time build and daily update scripts
```

## Point-in-time safety

Every row in the fundamental database has an `announce_date` column — the date BSE published the result. The API never returns data where `announce_date > query_date`, preventing future leakage.

```python
# Safe for backtesting — only uses data known on 2024-01-15
df = get_fundamentals("2024-01-15")
```

## License

MIT
