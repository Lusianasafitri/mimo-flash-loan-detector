#!/usr/bin/env python3
"""MiMo Flash Loan Detector - Real-time on-chain flash loan monitoring."""

import json
import time
import logging
import sqlite3
import hashlib
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field, asdict
from pathlib import Path
from collections import defaultdict

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("flash-loan-detector")

FLASH_LOAN_SIGNATURES = {
    "0x0b8c6674": {"protocol": "Aave V3", "function": "flashLoan"},
    "0x5cffe9de": {"protocol": "Aave V2", "function": "flashLoan"},
    "0xa415bcad": {"protocol": "dYdX", "function": "flashLoan"},
    "0xe0232b42": {"protocol": "Balancer", "function": "flashLoan"},
    "0x476343ee": {"protocol": "Aave V3", "function": "flashLoanSimple"},
    "0xab9c4b68": {"protocol": "MakerDAO", "function": "flashLoan"},
}

KNOWN_CONTRACTS = {
    "0x87870bca3f3fd6335c3f4ce8392d69350b4fa4e2": "Aave V3 Pool",
    "0x7d2768de32b0b80b7a3454c06bdac94a69ddc7a9": "Aave V2 LendingPool",
    "0xba12222222228d8ba445958a75a0704d566bf2c8": "Balancer Vault",
    "0x1111111254fb6c44bac0bed2854e76f90643097d": "1inch Router",
    "0xdef1c0ded9bec7f1a1670819833240f027b25eff": "0x Exchange Proxy",
    "0xe66b31678d6c16e9ebf358268a790b763c133750": "0x Coinbase Proxy",
    "0xa57bd0131866b0e5d46b705c5040e6382e775024": "Kyber Swap",
}


@dataclass
class FlashLoanEvent:
    tx_hash: str
    block_number: int
    timestamp: int
    protocol: str
    function: str
    borrower: str
    target_contract: str
    assets: List[str]
    amounts: List[str]
    fees: List[str]
    gas_used: int = 0
    gas_price: int = 0
    eth_value: float = 0.0
    status: str = "detected"
    profit_estimate: float = 0.0
    arbitrage_detected: bool = False
    liquidation_detected: bool = False
    metadata: Dict = field(default_factory=dict)

    @property
    def total_gas_cost_eth(self) -> float:
        return (self.gas_used * self.gas_price) / 1e18

    @property
    def tx_hash_short(self) -> str:
        return f"{self.tx_hash[:10]}...{self.tx_hash[-6:]}"

    def to_dict(self) -> Dict:
        d = asdict(self)
        d["tx_hash_short"] = self.tx_hash_short
        d["total_gas_cost_eth"] = self.total_gas_cost_eth
        return d


class Database:
    def __init__(self, db_path: str = "flash_loans.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS flash_loans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tx_hash TEXT UNIQUE NOT NULL,
                block_number INTEGER NOT NULL,
                timestamp INTEGER NOT NULL,
                protocol TEXT NOT NULL,
                function TEXT,
                borrower TEXT,
                target_contract TEXT,
                assets TEXT,
                amounts TEXT,
                fees TEXT,
                gas_used INTEGER DEFAULT 0,
                gas_price INTEGER DEFAULT 0,
                eth_value REAL DEFAULT 0,
                status TEXT DEFAULT 'detected',
                profit_estimate REAL DEFAULT 0,
                arbitrage_detected BOOLEAN DEFAULT 0,
                liquidation_detected BOOLEAN DEFAULT 0,
                metadata TEXT DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_block ON flash_loans(block_number)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_protocol ON flash_loans(protocol)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_borrower ON flash_loans(borrower)")
            conn.execute("""CREATE TABLE IF NOT EXISTS scan_progress (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                last_block INTEGER DEFAULT 0,
                total_scanned INTEGER DEFAULT 0,
                total_found INTEGER DEFAULT 0,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
            conn.execute("INSERT OR IGNORE INTO scan_progress (id) VALUES (1)")

    def insert_event(self, event: FlashLoanEvent) -> bool:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO flash_loans (tx_hash, block_number, timestamp, protocol, "
                    "function, borrower, target_contract, assets, amounts, fees, gas_used, gas_price, "
                    "eth_value, status, profit_estimate, arbitrage_detected, liquidation_detected, metadata) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (event.tx_hash, event.block_number, event.timestamp, event.protocol,
                     event.function, event.borrower, event.target_contract,
                     json.dumps(event.assets), json.dumps(event.amounts), json.dumps(event.fees),
                     event.gas_used, event.gas_price, event.eth_value, event.status,
                     event.profit_estimate, int(event.arbitrage_detected),
                     int(event.liquidation_detected), json.dumps(event.metadata))
                )
                return conn.total_changes > 0
        except sqlite3.IntegrityError:
            return False

    def update_progress(self, block_number: int, found: int = 0):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE scan_progress SET last_block = ?, total_scanned = total_scanned + 1, "
                "total_found = total_found + ?, last_updated = CURRENT_TIMESTAMP WHERE id = 1",
                (block_number, found)
            )

    def get_progress(self) -> Dict:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT * FROM scan_progress WHERE id = 1").fetchone()
            if row:
                return {"last_block": row[1], "total_scanned": row[2], "total_found": row[3]}
            return {"last_block": 0, "total_scanned": 0, "total_found": 0}

    def get_events(self, limit: int = 100, protocol: str = None) -> List[Dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            query = "SELECT * FROM flash_loans"
            params = []
            if protocol:
                query += " WHERE protocol = ?"
                params.append(protocol)
            query += " ORDER BY block_number DESC LIMIT ?"
            params.append(limit)
            return [dict(row) for row in conn.execute(query, params).fetchall()]

    def get_stats(self) -> Dict:
        with sqlite3.connect(self.db_path) as conn:
            total = conn.execute("SELECT COUNT(*) FROM flash_loans").fetchone()[0]
            protocols = dict(conn.execute(
                "SELECT protocol, COUNT(*) FROM flash_loans GROUP BY protocol"
            ).fetchall())
            unique_borrowers = conn.execute(
                "SELECT COUNT(DISTINCT borrower) FROM flash_loans"
            ).fetchone()[0]
            arb_count = conn.execute(
                "SELECT COUNT(*) FROM flash_loans WHERE arbitrage_detected = 1"
            ).fetchone()[0]
            return {
                "total_events": total,
                "protocols": protocols,
                "unique_borrowers": unique_borrowers,
                "arbitrage_detected": arb_count,
            }


class RPCClient:
    def __init__(self, rpc_url: str):
        self.rpc_url = rpc_url
        self._request_id = 0

    def _call(self, method: str, params: list, timeout: int = 30) -> Optional[Dict]:
        import urllib.request
        self._request_id += 1
        payload = json.dumps({
            "jsonrpc": "2.0", "method": method,
            "params": params, "id": self._request_id
        }).encode()
        req = urllib.request.Request(
            self.rpc_url, data=payload,
            headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except Exception as e:
            logger.error(f"RPC {method} failed: {e}")
            return None

    def get_block(self, block_num: int) -> Optional[Dict]:
        result = self._call("eth_getBlockByNumber", [hex(block_num), True])
        return result.get("result") if result else None

    def get_receipt(self, tx_hash: str) -> Optional[Dict]:
        result = self._call("eth_getTransactionReceipt", [tx_hash])
        return result.get("result") if result else None

    def get_latest_block(self) -> int:
        result = self._call("eth_blockNumber", [])
        if result and result.get("result"):
            return int(result["result"], 16)
        return 0


class FlashLoanDetector:
    def __init__(self, rpc_url: str = "https://eth-mainnet.g.alchemy.com/v2/demo",
                 db_path: str = "flash_loans.db"):
        self.rpc = RPCClient(rpc_url)
        self.db = Database(db_path)
        self.alerts: List[Dict] = []
        self._callbacks = []

    def on_flash_loan(self, callback):
        self._callbacks.append(callback)

    def _notify(self, event: FlashLoanEvent):
        for cb in self._callbacks:
            try:
                cb(event)
            except Exception as e:
                logger.error(f"Callback error: {e}")

    def _decode_input(self, input_data: str) -> Optional[Dict]:
        if len(input_data) < 10:
            return None
        method_sig = input_data[:10]
        if method_sig in FLASH_LOAN_SIGNATURES:
            info = FLASH_LOAN_SIGNATURES[method_sig].copy()
            info["method_sig"] = method_sig
            return info
        return None

    def _parse_logs(self, logs: List[Dict]) -> Tuple[List[str], List[str], List[str]]:
        assets, amounts, fees = [], [], []
        for log in logs:
            topics = log.get("topics", [])
            if len(topics) >= 3:
                assets.append(log.get("address", "unknown"))
                try:
                    amount = int(log.get("data", "0x0")[:66], 16)
                    amounts.append(str(amount))
                except (ValueError, IndexError):
                    amounts.append("0")
                fees.append("0")
        return assets or ["unknown"], amounts or ["0"], fees or ["0"]

    def _detect_patterns(self, event: FlashLoanEvent, all_txs_in_block: List[Dict]) -> FlashLoanEvent:
        same_borrower_txs = [
            tx for tx in all_txs_in_block
            if isinstance(tx, dict) and tx.get("from", "").lower() == event.borrower.lower()
            and tx.get("hash", "") != event.tx_hash
        ]
        if len(same_borrower_txs) > 2:
            event.arbitrage_detected = True
            event.metadata["related_txs"] = [tx.get("hash", "")[:16] for tx in same_borrower_txs[:5]]

        value_changes = []
        for tx in same_borrower_txs:
            try:
                val = int(tx.get("value", "0x0"), 16) / 1e18
                value_changes.append(val)
            except (ValueError, TypeError):
                pass
        if value_changes:
            net = sum(value_changes)
            if net > 0:
                event.profit_estimate = net
                event.metadata["estimated_profit_source"] = "value_diff"

        return event

    def analyze_transaction(self, tx: Dict, block: Dict, block_txs: List[Dict]) -> Optional[FlashLoanEvent]:
        input_data = tx.get("input", "0x")
        decoded = self._decode_input(input_data)
        if not decoded:
            return None

        receipt = self.rpc.get_receipt(tx["hash"])
        logs = receipt.get("logs", []) if receipt else []
        assets, amounts, fees = self._parse_logs(logs)
        gas_used = int(receipt.get("gasUsed", "0x0"), 16) if receipt else 0
        gas_price = int(tx.get("gasPrice", "0x0"), 16)
        timestamp = int(block.get("timestamp", "0x0"), 16)
        block_number = int(block["number"], 16)
        eth_value = int(tx.get("value", "0x0"), 16) / 1e18

        target = tx.get("to", "").lower()
        target_name = KNOWN_CONTRACTS.get(target, target)

        event = FlashLoanEvent(
            tx_hash=tx["hash"], block_number=block_number, timestamp=timestamp,
            protocol=decoded["protocol"], function=decoded["function"],
            borrower=tx.get("from", "unknown"), target_contract=target_name,
            assets=assets, amounts=amounts, fees=fees,
            gas_used=gas_used, gas_price=gas_price, eth_value=eth_value,
        )

        event = self._detect_patterns(event, block_txs)
        return event

    def scan_block(self, block_number: int) -> List[FlashLoanEvent]:
        block = self.rpc.get_block(block_number)
        if not block:
            return []

        txs = block.get("transactions", [])
        events = []
        for tx in txs:
            if isinstance(tx, dict):
                event = self.analyze_transaction(tx, block, txs)
                if event:
                    saved = self.db.insert_event(event)
                    if saved:
                        events.append(event)
                        self._notify(event)
                        logger.info(
                            f"[{event.protocol}] {event.tx_hash_short} | "
                            f"Block {block_number} | Borrower: {event.borrower[:10]}... | "
                            f"Gas: {event.gas_used:,}"
                        )

        self.db.update_progress(block_number, len(events))
        return events

    def scan_range(self, start_block: int, end_block: int,
                   batch_size: int = 10, delay: float = 0.15) -> Dict:
        logger.info(f"Scanning blocks {start_block:,} → {end_block:,} ({end_block - start_block + 1} blocks)")
        total_events = []
        start_time = time.time()

        for block_num in range(start_block, end_block + 1):
            events = self.scan_block(block_num)
            total_events.extend(events)

            if (block_num - start_block) % batch_size == 0:
                elapsed = time.time() - start_time
                progress = (block_num - start_block + 1) / (end_block - start_block + 1) * 100
                rate = (block_num - start_block + 1) / max(elapsed, 0.1)
                logger.info(
                    f"Progress: {progress:.1f}% | Block {block_num:,} | "
                    f"Found: {len(total_events)} | Rate: {rate:.1f} blocks/s"
                )

            time.sleep(delay)

        elapsed = time.time() - start_time
        return {
            "blocks_scanned": end_block - start_block + 1,
            "events_found": len(total_events),
            "elapsed_seconds": round(elapsed, 2),
            "rate_blocks_per_sec": round((end_block - start_block + 1) / max(elapsed, 0.1), 2),
            "events": [e.to_dict() for e in total_events],
        }

    def get_dashboard(self) -> Dict:
        progress = self.db.get_progress()
        stats = self.db.get_stats()
        recent = self.db.get_events(limit=10)
        return {
            "scan_progress": progress,
            "statistics": stats,
            "recent_events": recent,
            "alert_count": len(self.alerts),
        }

    def export_events(self, filepath: str, format: str = "json"):
        events = self.db.get_events(limit=10000)
        if format == "json":
            with open(filepath, "w") as f:
                json.dump(events, f, indent=2)
        elif format == "csv":
            import csv
            with open(filepath, "w", newline="") as f:
                if events:
                    writer = csv.DictWriter(f, fieldnames=events[0].keys())
                    writer.writeheader()
                    writer.writerows(events)
        logger.info(f"Exported {len(events)} events to {filepath}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="MiMo Flash Loan Detector")
    parser.add_argument("--rpc", default="https://eth-mainnet.g.alchemy.com/v2/demo")
    parser.add_argument("--start-block", type=int, help="Start block number")
    parser.add_argument("--end-block", type=int, help="End block number")
    parser.add_argument("--latest", action="store_true", help="Scan latest 100 blocks")
    parser.add_argument("--output", default="flash_loans.json")
    parser.add_argument("--db", default="flash_loans.db")
    parser.add_argument("--dashboard", action="store_true")
    args = parser.parse_args()

    detector = FlashLoanDetector(rpc_url=args.rpc, db_path=args.db)

    def on_event(event):
        if event.arbitrage_detected:
            logger.warning(f"ARB DETECTED: {event.tx_hash_short} ({event.protocol})")

    detector.on_flash_loan(on_event)

    if args.dashboard:
        dash = detector.get_dashboard()
        print(json.dumps(dash, indent=2))
    elif args.start_block and args.end_block:
        result = detector.scan_range(args.start_block, args.end_block)
        print(f"\nScan complete: {result['events_found']} flash loans in {result['blocks_scanned']} blocks")
        detector.export_events(args.output)
    elif args.latest:
        latest = detector.rpc.get_latest_block()
        if latest:
            result = detector.scan_range(latest - 100, latest)
            print(f"\nScan complete: {result['events_found']} flash loans in {result['blocks_scanned']} blocks")
    else:
        print("Use --start-block/--end-block or --latest. Run --dashboard for stats.")


if __name__ == "__main__":
    main()
