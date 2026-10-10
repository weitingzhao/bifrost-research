"""Exact-manifest repair command for an approved Research writer action.

Defaults to agent_reader dry run; --execute uses the domain writer environment.
Writes private local receipts, never dataset dumps into the public repository.
"""
import argparse
import json
from pathlib import Path

from bifrost_research.engines.volatility.atm_repair import load_manifest, repair


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--manifest', type=Path, required=True)
    p.add_argument('--sha256', required=True)
    p.add_argument('--receipt', type=Path, required=True)
    p.add_argument('--execute', action='store_true')
    a = p.parse_args()
    keys = load_manifest(a.manifest, a.sha256)
    if a.execute:
        from bifrost_research.db.conn import connect
        conn = connect()
    else:
        import psycopg2
        conn = psycopg2.connect(host='192.168.10.73', port=30432, user='agent_reader',
            dbname='bifrost_golden_source', connect_timeout=10,
            options='-c default_transaction_read_only=on -c statement_timeout=20000 -c jit=off')
    try:
        report = repair(conn, keys, execute=a.execute)
        report['manifest_sha256'] = a.sha256
        report['status'] = 'validated_not_committed' if a.execute else 'dry_run'
        a.receipt.write_text(json.dumps(report, default=str, indent=2))
        if a.execute:
            conn.commit()
            report['status'] = 'committed'
            a.receipt.write_text(json.dumps(report, default=str, indent=2))
        report.pop('rows')
        print(json.dumps(report))
    finally:
        if not conn.closed:
            conn.rollback()
        conn.close()


if __name__ == '__main__':
    main()
