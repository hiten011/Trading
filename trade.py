"""
Trade Executor - powered by OpenAlgo
--------------------------------------
Prerequisites:
  1. Start the OpenAlgo server:   ./start_server.sh
  2. Open http://127.0.0.1:5000  in your browser
  3. Log in with your Zerodha account
  4. Go to API Keys section and copy your OpenAlgo API key
  5. Set OPENALGO_API_KEY in your .env file

Usage:
  python trade.py
"""

import os
import sys
from dotenv import load_dotenv
from openalgo import api

load_dotenv()

OPENALGO_API_KEY = os.getenv("OPENALGO_API_KEY")
OPENALGO_HOST    = os.getenv("OPENALGO_HOST", "http://127.0.0.1:5000")


def get_client() -> api:
    if not OPENALGO_API_KEY:
        print("ERROR: OPENALGO_API_KEY not set in .env")
        print("  1. Open http://127.0.0.1:5000 and log in")
        print("  2. Go to API Keys, copy your key")
        print("  3. Add OPENALGO_API_KEY=<your-key> to .env")
        sys.exit(1)
    return api(api_key=OPENALGO_API_KEY, host=OPENALGO_HOST)


def buy(symbol: str, quantity: int, exchange: str = "NSE",
        product: str = "CNC", strategy: str = "python") -> dict:
    client = get_client()
    print(f"\nPlacing BUY order: {quantity} x {symbol} on {exchange} ...")
    response = client.placeorder(
        strategy=strategy,
        symbol=symbol,
        exchange=exchange,
        action="BUY",
        price_type="MARKET",
        product=product,
        quantity=quantity,
    )
    if response.get("status") == "success":
        order_id = response.get("orderid")
        print(f"Order placed successfully! Order ID: {order_id}")
        return response
    else:
        print(f"Order failed: {response}")
        sys.exit(1)


def sell(symbol: str, quantity: int, exchange: str = "NSE",
         product: str = "CNC", strategy: str = "python") -> dict:
    client = get_client()
    print(f"\nPlacing SELL order: {quantity} x {symbol} on {exchange} ...")
    response = client.placeorder(
        strategy=strategy,
        symbol=symbol,
        exchange=exchange,
        action="SELL",
        price_type="MARKET",
        product=product,
        quantity=quantity,
    )
    if response.get("status") == "success":
        order_id = response.get("orderid")
        print(f"Order placed successfully! Order ID: {order_id}")
        return response
    else:
        print(f"Order failed: {response}")
        sys.exit(1)


def get_order_status(order_id: str, strategy: str = "python") -> dict:
    client = get_client()
    return client.orderstatus(order_id=order_id, strategy=strategy)


if __name__ == "__main__":
    # Buy 1 share of GOLDIETF on NSE (CNC = delivery)
    response = buy(
        symbol="GOLDIETF",
        quantity=1,
        exchange="NSE",
        product="CNC",
    )

    order_id = response.get("orderid")
    if order_id:
        status = get_order_status(order_id)
        data = status.get("data", {})
        print(f"\nOrder Status : {data.get('order_status', 'N/A')}")
        print(f"Symbol       : {data.get('tradingsymbol', 'N/A')}")
        print(f"Quantity     : {data.get('quantity', 'N/A')}")
        print(f"Price        : {data.get('average_price', 'pending')}")
