# VOLKOVX

Polymarket trading bot suite — two independent systems in one repo.

## Project layout

```
VOLKOVX/
└── VOLKOVX_V4/
    ├── __init__.py              ← package entry — exports V2 arbitrage API
    │
    ├── bregman_projection.py    ┐
    ├── frank_wolfe_solver.py    │  Bot 1: ARBITRAGE ENGINE
    ├── engine_v2.py             │  (any binary Polymarket market)
    │                            ┘
    ├── volkovx/                 ┐
    │   ├── __init__.py          │  Bot 2: BTC 5-MIN UP/DOWN SNIPER
    │   ├── __main__.py          │  (specialised, runs as `python -m volkovx`)
    │   ├── config.py            │
    │   ├── feeds.py             │
    │   ├── filters.py           │
    │   ├── executor.py          │
    │   ├── session.py           │
    │   ├── dashboard.py         │
    │   └── risk.py              │
    │                            ┘
    ├── analyze.py               ← post-trade analyser (sniper)
    ├── generate_api_creds.py    ← Polymarket CLOB key bootstrap
    ├── requirements.txt
    │
    ├── tests/
    │   ├── conftest.py
    │   ├── test_volkovx.py      ← 32 tests for arbitrage engine
    │   └── test_core.py         ← 30 tests for BTC sniper
    │
    ├── AUDIT_REPORT.md          ← deep-audit findings + fixes
    ├── DEPLOYMENT_GUIDE.md      ← DRY → CANARY → LIVE runbook
    ├── README.md                ← this file (top level)
    └── BTC_SNIPER_README.md     ← extra notes on the BTC bot
```

## Quick start

```bash
cd VOLKOVX_V4
pip install -r requirements.txt

# Run all 62 tests
pytest tests/ -v

# Run the BTC sniper (DRY mode by default)
cp .env.example .env             # then edit .env
python -m volkovx
```

## Status

✅ All Python files compile (`python -m py_compile`)
✅ All 62 unit tests PASS
✅ `import VOLKOVX_V4` works end-to-end

See `VOLKOVX_V4/AUDIT_REPORT.md` for the complete code-review notes.
