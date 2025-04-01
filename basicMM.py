from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, OpenOrderParams, TradeParams
from py_clob_client.order_builder.constants import BUY, SELL
import time
import datetime
import pandas as pd
import os


class PolymarketLiquidityBot:
    def __init__(self, host, key, chain_id=137):
        self.client = ClobClient(host, key=key, chain_id=chain_id)
        self.orders = []
        self.markets = []
        self.pnl_history = []

    def create_api_key(self):
        try:
            return self.client.create_api_key()
        except Exception as e:
            print(f"Error creating API key: {e}")
            return None

    def get_active_markets(self):
        """Fetch available sampling markets with rewards enabled"""
        try:
            response = self.client.get_sampling_simplified_markets(next_cursor="")
            self.markets = response['data']

            # Add synthetic descriptions using available fields
            for market in self.markets:
                outcomes = [t['outcome'] for t in market['tokens']]
                market['description'] = f"Market {market['condition_id'][-6:]} - Outcomes: {', '.join(outcomes)}"

            return self.markets
        except Exception as e:
            print(f"Error fetching sampling markets: {e}")
            return []

    def get_mid_price(self, market_id, token_id):
        try:
            orders = self.client.get_orders(
                OpenOrderParams(
                    market=market_id,
                    asset_id=token_id
                )
            )

            # Find best bid and ask
            bids = [order for order in orders if order['side'] == 'buy']
            asks = [order for order in orders if order['side'] == 'sell']

            if not bids and not asks:
                return 0.5  # Default to 0.5 if no orders exist

            if not bids:
                best_bid_price = 0
            else:
                best_bid = max(bids, key=lambda x: float(x['price']))
                best_bid_price = float(best_bid['price'])

            if not asks:
                best_ask_price = 1
            else:
                best_ask = min(asks, key=lambda x: float(x['price']))
                best_ask_price = float(best_ask['price'])

            # Calculate mid price
            mid_price = (best_bid_price + best_ask_price) / 2
            return mid_price
        except Exception as e:
            print(f"Error getting mid price: {e}")
            return 0.5  # Default to 0.5 if there's an error

    def place_limit_order(self, market_id, token_id, side, size, price, fee_rate_bps=0):
        """Place a limit order on Polymarket"""
        try:
            # Make sure price and size are valid
            price = max(0.01, min(0.99, price))
            min_size = self.get_market_min_size(market_id)
            size = max(float(min_size), size)

            order_args = OrderArgs(
                price=price,
                size=size,
                side=side,
                token_id=token_id,
                fee_rate_bps=fee_rate_bps
            )

            signed_order = self.client.create_order(order_args)
            resp = self.client.post_order(signed_order, OrderType.GTC)

            # Store order details if successful
            if resp.get('orderID'):
                order_info = {
                    'id': resp['orderID'],
                    'market_id': market_id,
                    'token_id': token_id,
                    'side': side,
                    'size': size,
                    'price': price,
                    'time': datetime.datetime.now().isoformat(),
                    'status': 'live'
                }
                self.orders.append(order_info)
                print(f"Placed {side} order {resp['orderID']} at price {price}")
            else:
                print(f"Failed to place order: {resp}")

            return resp
        except Exception as e:
            print(f"Error placing order: {e}")
            return {'success': False, 'errorMsg': str(e)}

    def cancel_order(self, order_id):
        """Cancel an existing order"""
        try:
            resp = self.client.cancel(order_id=order_id)

            # Update order status
            for order in self.orders:
                if order['id'] == order_id and order_id in resp.get('canceled', []):
                    order['status'] = 'canceled'

            return resp
        except Exception as e:
            print(f"Error canceling order: {e}")
            return {'success': False, 'errorMsg': str(e)}

    def run_strategy(self, market_id, token_id, risk_amount, max_spread=0.03, duration_minutes=60):
        """Run the liquidity provision strategy for a specified duration"""
        print(f"Starting strategy for market {market_id} with risk amount {risk_amount}")

        start_time = time.time()
        end_time = start_time + (duration_minutes * 60)

        # Calculate order size based on risk amount
        size = risk_amount / 2  # Split risk between buy and sell sides

        active_buy_order = None
        active_sell_order = None

        while time.time() < end_time:
            try:
                # Get current mid price
                mid_price = self.get_mid_price(market_id, token_id)

                if mid_price:
                    # Set buy price slightly below mid
                    buy_price = max(0.01, round(mid_price - max_spread, 2))

                    # Set sell price slightly above mid
                    sell_price = min(0.99, round(mid_price + max_spread, 2))

                    # Cancel existing buy order if it exists
                    if active_buy_order:
                        self.cancel_order(active_buy_order['id'])
                        active_buy_order = None

                    # Cancel existing sell order if it exists
                    if active_sell_order:
                        self.cancel_order(active_sell_order['id'])
                        active_sell_order = None

                    # Place new buy order
                    buy_resp = self.place_limit_order(market_id, token_id, BUY, size, buy_price)
                    if buy_resp.get('orderID'):
                        active_buy_order = next((o for o in self.orders if o['id'] == buy_resp['orderID']), None)

                    # Place new sell order
                    sell_resp = self.place_limit_order(market_id, token_id, SELL, size, sell_price)
                    if sell_resp.get('orderID'):
                        active_sell_order = next((o for o in self.orders if o['id'] == sell_resp['orderID']), None)

                    print(f"Placed orders at mid price {mid_price}: BUY @ {buy_price}, SELL @ {sell_price}")

                # Wait 30 seconds before refreshing orders
                time.sleep(30)
            except Exception as e:
                print(f"Error in strategy loop: {e}")
                time.sleep(5)  # Wait a bit and try again

        # Cancel all orders at the end
        if active_buy_order:
            self.cancel_order(active_buy_order['id'])
        if active_sell_order:
            self.cancel_order(active_sell_order['id'])

        print("Strategy completed")

    def get_pnl(self):
        """Calculate profit and loss from filled orders and track historical values"""
        try:
            # Get trades to track fills
            trades = self.client.get_trades(
                TradeParams(
                    maker_address=self.client.get_address()
                )
            )

            # Calculate current P&L
            buy_volume = 0
            buy_cost = 0
            sell_volume = 0
            sell_revenue = 0

            for trade in trades:
                if trade['status'] == 'CONFIRMED':
                    size = float(trade['size'])
                    price = float(trade['price'])

                    if trade['side'] == 'buy':
                        buy_volume += size
                        buy_cost += size * price
                    else:  # sell
                        sell_volume += size
                        sell_revenue += size * (1 - price)

            # Calculate P&L
            realized_pnl = sell_revenue - buy_cost

            # Track reward payments (placeholder)
            reward_earnings = self.get_rewards_total()

            total_pnl = realized_pnl + reward_earnings

            # Add to history with timestamp
            self.pnl_history.append({
                'timestamp': datetime.datetime.now().isoformat(),
                'realized_pnl': realized_pnl,
                'rewards': reward_earnings,
                'total_pnl': total_pnl
            })

            return self.pnl_history
        except Exception as e:
            print(f"Error calculating P&L: {e}")
            return []

    def get_rewards_total(self):
        """Get the total rewards earned from the liquidity rewards program"""
        # Placeholder - in a real implementation, you would query Polymarket's API
        # for rewards information. Rewards are paid daily around midnight UTC.
        return 0


import dash
import plotly.graph_objs as go
from dash import Dash, dcc, html, Input, Output
import time



def create_dashboard(bot):
    """Create a Dash dashboard with a searchable dropdown for markets."""
    app = Dash(__name__)

    # Fetch active markets
    bot.get_active_markets()

    # Prepare market data with synthetic names
    market_data = [
        {
            'id': market['condition_id'],
            'name': f"{' vs '.join([t['outcome'] for t in market['tokens']])}",
            'full_info': market
        }
        for market in bot.markets
    ]

    app.layout = html.Div([
        html.H1("Polymarket Liquidity Bot Dashboard"),

        html.Div([
            html.H2("Search & Select Market"),
            dcc.Input(
                id='market-search',
                type='text',
                placeholder='Search markets by name...',
                style={'width': '100%', 'marginBottom': '10px'}
            ),
            dcc.Dropdown(
                id='market-dropdown',
                options=[
                    {'label': f"{m['name']} (Market {m['id'][-6:]})", 'value': m['id']}
                    for m in market_data
                ],
                value=market_data[0]['id'] if market_data else None,
                placeholder="Select a market",
                searchable=True,
                style={
                    'maxHeight': '200px',
                    'overflowY': 'auto',
                    'fontSize': '14px'
                }
            ),
            html.Div(id='market-details',
                     style={'marginTop': '10px', 'padding': '8px',
                            'backgroundColor': '#f0f0f0', 'whiteSpace': 'pre-wrap'})
        ], style={'margin': '20px 0', 'padding': '20px', 'border': '1px solid #ddd'}),
    ])

    @app.callback(
        [Output('market-dropdown', 'options'),
         Output('market-details', 'children')],
        [Input('market-search', 'value'),
         Input('market-dropdown', 'value')],
    )
    def update_market_display(search_query, selected_id):
        # Filter markets based on search query
        filtered_options = [
            {
                'label': f"{m['name']} (Market {m['id'][-6:]})",
                'value': m['id']
            }
            for m in market_data
            if not search_query or search_query.lower() in m['name'].lower()
        ]

        # Show details for selected market
        if selected_id:
            market = next((m for m in market_data if m['id'] == selected_id), None)
            if market:
                rewards = market['full_info'].get('rewards', {})
                details = [
                    f"Market ID: {market['id']}",
                    f"Active: {market['full_info'].get('active', False)}",
                    f"Closed: {market['full_info'].get('closed', False)}",
                    f"Min Size: {rewards.get('min_size', 'N/A')}",
                    f"Max Spread: {rewards.get('max_spread', 'N/A')}¢",
                    "Outcomes:",
                    *[f"- {t['outcome']}" for t in market['full_info']['tokens']]
                ]
                return [filtered_options, html.Div([html.P(line) for line in details])]

        return [filtered_options, "Select a market to view details"]

    return app


def main():
    # Load configuration
    os.environ["POLYMARKET_API_KEY"] = "374ba92c7cdddfabc9130e2e52070a83611e761da68b10e853a2e7465a9bafec"
    host = os.getenv("POLYMARKET_HOST", "https://clob.polymarket.com")
    key = os.getenv("POLYMARKET_API_KEY", "")
    chain_id = int(os.getenv("POLYMARKET_CHAIN_ID", "137"))

    if not key:
        print("API key not provided. Please set the POLYMARKET_API_KEY environment variable.")
        return

    # Initialize bot
    bot = PolymarketLiquidityBot(host, key, chain_id)

    # Get available markets
    markets = bot.get_active_markets()
    print(f"Found {len(markets)} markets")

    # Create and run dashboard
    app = create_dashboard(bot)
    app.run(debug=True, port=8050)



if __name__ == "__main__":
    main()

