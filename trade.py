"""
Kite Trade Executor
-------------------
Places orders using a saved access_token.
Run auth.py first to generate the access_token.
"""

import os
import sys
from kiteconnect import KiteConnect
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("KITE_API_KEY")
TOKEN_FILE = "access_token.txt"


def load_access_token() -> str:
    if not os.path.exists(TOKEN_FILE):
        print("ERROR: access_token.txt not found.")
        print("Please run `python auth.py` first to log in.")
        sys.exit(1)
    with open(TOKEN_FILE, "r") as f:
        return f.read().strip()


def get_kite() -> KiteConnect:
    kite = KiteConnect(api_key=API_KEY)
    kite.set_access_token(load_access_token())
    return kite


def buy(symbol: str, quantity: int, exchange: str = "NSE") -> dict:
    kite = get_kite()
    print(f"\nPlacing BUY order: {quantity} x {symbol} on {exchange} ...")
    order_id = kite.place_order(
        variety=kite.VARIETY_REGULAR,
        exchange=exchange,
        tradingsymbol=symbol,
        transaction_type=kite.TRANSACTION_TYPE_BUY,
        quantity=quantity,
        product=kite.PRODUCT_CNC,       # CNC = delivery (equity/ETF)
        order_type=kite.ORDER_TYPE_MARKET,
    )
    print(f"Order placed successfully! Order ID: {order_id}")
    return order_id


def get_order_status(order_id: str) -> dict:
    kite = get_kite()
    orders = kite.orders()
    for order in orders:
        if str(order["order_id"]) == str(order_id):
            return order
    return {}


if __name__ == "__main__":
    # Buy 1 share of GOLDIETF on NSE
    SYMBOL = "GOLDIETF"
    QUANTITY = 1
    EXCHANGE = "NSE"

    order_id = buy(SYMBOL, QUANTITY, EXCHANGE)

    # Show order status
    status = get_order_status(order_id)
    if status:
        print(f"\nOrder Status : {status.get('status')}")
        print(f"Symbol       : {status.get('tradingsymbol')}")
        print(f"Quantity     : {status.get('quantity')}")
        print(f"Order Type   : {status.get('order_type')}")
        print(f"Price        : {status.get('average_price', 'pending')}")
