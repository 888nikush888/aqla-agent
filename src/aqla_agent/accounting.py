"""Slot accounting over observed positions and durable own reservations."""


def occupied_markets(data, account, *, exclude_trade=None):
    markets = {symbol for symbol, size in (account.positions or {}).items() if size}
    markets.update(row['market'] for row in account.entry_orders)
    markets.update(row['plan']['market']['symbol'] for identity, row in data['trades'].items()
                   if identity != exclude_trade and row['status'] in ('waiting_entry','preparing','opening','open','protecting','unknown','closing','cleanup','conflicted'))
    return markets


def foreign_entry_orders(data, account, symbol):
    ours = set()
    own_cloids = set()
    for row in data['trades'].values():
        if row['status'] not in ('waiting_entry','preparing','opening','open','protecting','unknown','closing','cleanup','conflicted'):
            continue
        if row.get('entry_cloid'):
            own_cloids.add(row['entry_cloid'])
        for entry in row.get('entry_orders', []):
            own_cloids.add(entry['cloid'])
            if (oid := row.get('order_observations', {}).get(entry['cloid'], {}).get('oid')) is not None:
                ours.add(oid)
        observed = row.get('order_observations', {}).get(row.get('entry_cloid'), {})
        if observed.get('oid') is not None:
            ours.add(observed['oid'])
    return [row for row in account.entry_orders if row['market'] == symbol
            and row.get('cloid') not in own_cloids and row.get('oid') not in ours]


def inventory_view(data, account, limit):
    own = {row['plan']['market']['symbol'] for row in data['trades'].values()
           if row['status'] in ('waiting_entry','preparing','opening','open','protecting','unknown','closing','cleanup','conflicted')}
    observed = {symbol for symbol, size in (account.positions or {}).items() if size}
    reserved = {row['market'] for row in account.entry_orders}
    occupied = occupied_markets(data, account)
    waiting = {row['plan']['market']['symbol'] for row in data['trades'].values() if row['status']=='waiting_entry'}
    active_own = own - waiting
    return {'observed_at': account.observed_at, 'mode': account.account_mode, 'manual_markets': sorted(observed - active_own),
            'aqla_markets': sorted(active_own), 'reserved_markets': sorted((reserved | waiting) - observed - active_own),
            'waiting_entry_markets': sorted(waiting),
            'occupied_count': len(occupied), 'limit': limit, 'free_slots': max(0, limit - len(occupied)),
            'available_margin_usdc': str(account.available_margin), 'equity_usdc': str(account.equity),
            'conflicted_markets': sorted(row['plan']['market']['symbol'] for row in data['trades'].values() if row['status']=='conflicted')}
