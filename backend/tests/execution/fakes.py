"""Mock broker back-ends. No test in this suite talks to a real exchange."""
from types import SimpleNamespace

import ccxt


class FakeCcxt:
    id = "fakex"

    def __init__(self, sandbox=True, api_key="k", default_type="spot", withdrawals=None):
        self.apiKey = api_key
        self.options = {"defaultType": default_type}
        self.has = {"createOrder": True, "createMarketOrder": True, "createLimitOrder": True, "createStopMarketOrder": True,
                    "createStopLimitOrder": False, "fetchOHLCV": True, "fetchTicker": True, "fetchClosedOrders": True}
        self.timeframes = {"1m": "1m", "1h": "1h", "3d": "3d"}
        self.markets = {}
        self._sandbox_supported = sandbox
        self.sandbox = False
        self.orders = {}
        self.fail_next = []  # exceptions raised by upcoming create_order calls
        self.fail_after_accept = None  # raise AFTER the order was recorded (lost response)
        self.create_calls = 0
        self.now_ms = 1_700_000_000_000
        self.balance = {"USDT": {"total": 1000.0, "used": 0.0}, "total": {"USDT": 1000.0, "BTC": 0.5}}
        if withdrawals is not None:
            self.sapiGetAccountApiRestrictions = lambda: {"enableWithdrawals": withdrawals}

    def set_sandbox_mode(self, on):
        if not self._sandbox_supported:
            raise ccxt.NotSupported("no sandbox")
        self.sandbox = on

    def load_markets(self):
        self.markets = {
            "BTC/USDT": {"active": True, "limits": {"amount": {"min": 0.001}, "cost": {"min": 10}}},
            "ETH/USDT": {"active": False, "limits": {}},
        }

    def milliseconds(self):
        return self.now_ms

    def fetch_balance(self):
        return self.balance

    def fetch_ticker(self, symbol):
        return {"timestamp": self.now_ms, "bid": 29990.0, "ask": 30010.0, "last": 30000.0}

    def fetch_ohlcv(self, symbol, timeframe, limit=500):
        step = 3_600_000
        start = self.now_ms - self.now_ms % step - 4 * step
        return [[start + i * step, 100 + i, 101 + i, 99 + i, 100.5 + i, 10.0] for i in range(5)]  # last bar still forming

    def create_order(self, symbol, kind, side, amount, price, params):
        self.create_calls += 1
        if self.fail_next:
            raise self.fail_next.pop(0)
        cid = params["clientOrderId"]
        for o in self.orders.values():
            if o["clientOrderId"] == cid:
                raise ccxt.InvalidOrder("duplicate clientOrderId")
        oid = str(len(self.orders) + 1)
        self.orders[oid] = {"id": oid, "clientOrderId": cid, "symbol": symbol, "type": kind, "side": side, "amount": amount, "price": price,
                            "status": "open", "filled": 0.0, "average": None, "fee": None, "params": params}
        if self.fail_after_accept:
            exc, self.fail_after_accept = self.fail_after_accept, None
            raise exc
        return dict(self.orders[oid])

    def fill(self, oid, qty, price, fee=0.0):
        o = self.orders[oid]
        o["average"] = ((o["average"] or 0.0) * o["filled"] + price * qty) / (o["filled"] + qty)  # cumulative average, as exchanges report
        o["filled"] += qty
        o["fee"] = {"cost": ((o["fee"] or {}).get("cost") or 0.0) + fee}
        if o["filled"] >= o["amount"]:
            o["status"] = "closed"

    def fetch_order(self, oid, symbol):
        return dict(self.orders[oid])

    def cancel_order(self, oid, symbol):
        if self.orders[oid]["status"] != "open":
            raise ccxt.OrderNotFound("gone")
        self.orders[oid]["status"] = "canceled"

    def fetch_open_orders(self, symbol=None):
        return [dict(o) for o in self.orders.values() if o["status"] == "open"]

    def fetch_closed_orders(self, symbol=None, limit=50):
        return [dict(o) for o in self.orders.values() if o["status"] != "open"]


class FakeIB:
    """Mimics the subset of ib_async.IB used by IbkrAdapter."""

    def __init__(self, accounts=("DU123",)):
        self.accounts = list(accounts)
        self.connected = False
        self._trades = []
        self.fail_place = None
        self.reject = False
        self._next_id = 1

    def connect(self, host, port, clientId=1):
        self.connected = True

    def disconnect(self):
        self.connected = False

    def isConnected(self):
        return self.connected

    def managedAccounts(self):
        return self.accounts

    def qualifyContracts(self, contract):
        if contract.symbol == "BOGUS":
            return []
        contract.conId = abs(hash((contract.symbol, contract.secType))) % 10**6
        return [contract]

    def accountSummary(self):
        v = SimpleNamespace
        return [v(tag="NetLiquidation", value="50000", currency="EUR"), v(tag="TotalCashValue", value="20000", currency="EUR"),
                v(tag="InitMarginReq", value="5000", currency="EUR"), v(tag="NetLiquidation", value="54000", currency="USD")]

    def positions(self):
        return [SimpleNamespace(contract=t.contract, position=t.orderStatus.filled * (1 if t.order.action == "BUY" else -1),
                                avgCost=t.orderStatus.avgFillPrice * float(getattr(t.contract, "multiplier", "") or 1))
                for t in self._trades if t.orderStatus.filled]

    def placeOrder(self, contract, order):
        if self.fail_place:
            exc, self.fail_place = self.fail_place, None
            raise exc
        order.orderId = self._next_id
        self._next_id += 1
        status = SimpleNamespace(status="Inactive" if self.reject else "Submitted", filled=0.0, avgFillPrice=0.0)
        trade = SimpleNamespace(contract=contract, order=order, orderStatus=status, fills=[], log=[SimpleNamespace(message="margin" if self.reject else "")])
        self._trades.append(trade)
        return trade

    def fill(self, trade, qty, price, commission=1.0):
        trade.orderStatus.filled += qty
        trade.orderStatus.avgFillPrice = price
        trade.fills.append(SimpleNamespace(commissionReport=SimpleNamespace(commission=commission)))
        if trade.orderStatus.filled >= trade.order.totalQuantity:
            trade.orderStatus.status = "Filled"

    def cancelOrder(self, order):
        for t in self._trades:
            if t.order is order:
                t.orderStatus.status = "Cancelled"

    def trades(self):
        return list(self._trades)

    def openTrades(self):
        return [t for t in self._trades if t.orderStatus.status == "Submitted"]
