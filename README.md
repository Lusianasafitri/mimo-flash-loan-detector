# MiMo Flash Loan Detector

Real-time on-chain flash loan monitoring and anomaly detection tool. Built with **MiMo V2.5** agent framework.

## Features

- Detects flash loans across Aave V2/V3, dYdX, Balancer, MakerDAO
- Arbitrage pattern detection (multi-tx correlation)
- SQLite persistent storage with indexed queries
- Real-time alert system with callbacks
- CSV/JSON export for analysis
- Scan progress tracking and rate monitoring

## Installation

```bash
pip install -r requirements.txt
```

## Usage

```bash
# Scan specific block range
python detector.py --start-block 18000000 --end-block 18000100

# Scan latest 100 blocks
python detector.py --latest

# View dashboard
python detector.py --dashboard

# Export results
python detector.py --start-block 18000000 --end-block 18000100 --output results.csv
```

## Architecture

Built on MiMo V2.5's agent framework for autonomous blockchain monitoring. Uses direct RPC calls to analyze transaction input data and identify flash loan patterns by method signature matching. Supports multi-transaction correlation for arbitrage detection.

## Use Cases

- MEV research and protection
- DeFi protocol security monitoring
- Flash loan arbitrage analysis
- Oracle manipulation detection

## License

MIT
