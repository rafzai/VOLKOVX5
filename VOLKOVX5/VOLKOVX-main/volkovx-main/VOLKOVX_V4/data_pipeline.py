"""
Data Pipeline - Real-Time Market Monitoring
Combines WebSocket order book data + on-chain event confirmation

Latency Profile:
  WebSocket update: <5ms (real-time order book)
  Blockchain confirmation: ~2s (Polygon block time)
  Total detection-to-execution: 30ms (top traders)

Data Sources:
  1. Polymarket CLOB WebSocket: order book snapshots, trades, fills
  2. Alchemy Polygon RPC: OrderFilled events, transaction confirmation
  3. DEX aggregators: liquidity data from related markets
"""

import asyncio
import logging
import time
import json
from typing import Callable, Dict, Optional, List
from dataclasses import dataclass
from datetime import datetime
import websockets

logger = logging.getLogger(__name__)

@dataclass
class OrderBookSnapshot:
    """Real-time order book state"""
    timestamp: float
    market_id: str
    
    # Price data
    yes_price: float
    no_price: float
    mid_price: float
    
    # Liquidity (dollar amounts available)
    yes_liquidity: float
    no_liquidity: float
    
    # Order book depth
    bid_ask_spread: float
    
    # Metadata
    volume_24h: float
    traders_count: int

@dataclass
class MarketSnapshot:
    """Full market state"""
    market_id: str
    timestamp: float
    title: str
    description: str
    
    order_book: OrderBookSnapshot
    
    # Historical data
    open_price: float
    high_24h: float
    low_24h: float
    
    expires_at: int

class DataPipeline:
    """
    Real-time market data aggregation
    
    Responsibilities:
    1. Connect to WebSocket and receive order book updates
    2. Parse and validate data
    3. Detect significant market changes
    4. Trigger callbacks for arbitrage engine
    5. Maintain order book state
    6. Log all market events
    """
    
    def __init__(
        self,
        polymarket_ws_url: str,
        polymarket_api_key: str,
        alchemy_rpc_url: str,
        update_callback: Callable[[OrderBookSnapshot], None]
    ):
        self.polymarket_ws_url = polymarket_ws_url
        self.polymarket_api_key = polymarket_api_key
        self.alchemy_rpc_url = alchemy_rpc_url
        self.update_callback = update_callback
        
        # State
        self.connected = False
        self.market_snapshots: Dict[str, OrderBookSnapshot] = {}
        self.running = False
        self.ws_connection = None
    
    async def initialize(self):
        """Initialize data pipeline"""
        
        logger.info("🔌 Data pipeline initializing...")
        
        try:
            # In production: connect to WebSocket
            # For now: simulate connection
            self.connected = True
            logger.info("✅ Data pipeline ready")
        except Exception as e:
            logger.error(f"❌ Data pipeline initialization failed: {e}")
            raise
    
    async def run(self, duration_seconds: Optional[int] = None):
        """
        Run data pipeline
        
        Monitors markets and triggers callbacks on updates
        """
        
        self.running = True
        start_time = time.time()
        
        try:
            logger.info("🚀 Data pipeline started")
            
            # In production: connect to WebSocket
            # For demo: simulate market updates
            
            while self.running:
                elapsed = time.time() - start_time
                
                if duration_seconds and elapsed > duration_seconds:
                    logger.info(f"Duration reached: {elapsed:.1f}s")
                    break
                
                # Simulate market update (in production: WebSocket event)
                await self._simulate_market_update()
                
                # Check for significant changes
                await asyncio.sleep(0.5)  # Update every 500ms
        
        except asyncio.CancelledError:
            logger.info("Data pipeline cancelled")
        except Exception as e:
            logger.error(f"Data pipeline error: {e}")
        finally:
            self.running = False
    
    async def _simulate_market_update(self):
        """Simulate order book update for testing"""
        
        # Create mock snapshot
        snapshot = OrderBookSnapshot(
            timestamp=time.time(),
            market_id="test_market_1",
            
            yes_price=0.62,
            no_price=0.33,
            mid_price=0.475,
            
            yes_liquidity=500_000,
            no_liquidity=500_000,
            
            bid_ask_spread=0.02,
            
            volume_24h=1_000_000,
            traders_count=150
        )
        
        # Store snapshot
        self.market_snapshots[snapshot.market_id] = snapshot
        
        # Trigger callback (engine will detect arbitrage)
        if self.update_callback:
            await self.update_callback(snapshot)
    
    async def connect_websocket(self):
        """Connect to Polymarket WebSocket"""
        
        try:
            logger.info(f"Connecting to WebSocket: {self.polymarket_ws_url}")
            
            async with websockets.connect(self.polymarket_ws_url) as websocket:
                self.ws_connection = websocket
                self.connected = True
                
                logger.info("✅ WebSocket connected")
                
                # Subscribe to markets
                subscribe_msg = {
                    "type": "subscribe",
                    "topic": "orderbook",
                    "markets": "all"
                }
                await websocket.send(json.dumps(subscribe_msg))
                
                # Listen for updates
                async for message in websocket:
                    await self._handle_websocket_message(message)
        
        except Exception as e:
            logger.error(f"WebSocket error: {e}")
            self.connected = False
    
    async def _handle_websocket_message(self, message: str):
        """Process WebSocket message"""
        
        try:
            data = json.loads(message)
            
            if data.get("type") == "update":
                # Order book update
                await self._process_order_book_update(data)
            
            elif data.get("type") == "trade":
                # New trade
                await self._process_trade(data)
            
        except json.JSONDecodeError:
            logger.error(f"Invalid JSON: {message}")
        except Exception as e:
            logger.error(f"Message handling error: {e}")
    
    async def _process_order_book_update(self, data: Dict):
        """Process order book update"""
        
        market_id = data.get("market_id")
        
        snapshot = OrderBookSnapshot(
            timestamp=data.get("timestamp", time.time()),
            market_id=market_id,
            
            yes_price=data.get("yes_price", 0.5),
            no_price=data.get("no_price", 0.5),
            mid_price=(data.get("yes_price", 0.5) + data.get("no_price", 0.5)) / 2,
            
            yes_liquidity=data.get("yes_liquidity", 0),
            no_liquidity=data.get("no_liquidity", 0),
            
            bid_ask_spread=data.get("bid_ask_spread", 0),
            
            volume_24h=data.get("volume_24h", 0),
            traders_count=data.get("traders_count", 0)
        )
        
        # Store and trigger callback
        self.market_snapshots[market_id] = snapshot
        await self.update_callback(snapshot)
    
    async def _process_trade(self, data: Dict):
        """Process executed trade"""
        
        logger.debug(
            f"Trade executed:\n"
            f"   Market: {data.get('market_id')}\n"
            f"   Outcome: {data.get('outcome')}\n"
            f"   Price: ${data.get('price', 0):.4f}\n"
            f"   Size: {data.get('size', 0)}"
        )
    
    async def get_market_snapshot(self, market_id: str) -> Optional[OrderBookSnapshot]:
        """Get current market snapshot"""
        return self.market_snapshots.get(market_id)
    
    async def query_on_chain_events(self, market_id: str) -> List[Dict]:
        """Query on-chain OrderFilled events from Polygon"""
        
        # In production: use Alchemy RPC to query events
        # For now: return empty list
        
        logger.debug(f"Querying on-chain events for {market_id}")
        return []
    
    async def confirm_transaction(self, tx_hash: str) -> bool:
        """Confirm transaction was included in block"""
        
        # In production: poll Polygon until confirmation
        # For now: return True
        
        return True
    
    def get_market_list(self) -> List[str]:
        """Get list of monitored markets"""
        return list(self.market_snapshots.keys())
    
    async def cleanup(self):
        """Cleanup resources"""
        
        self.running = False
        
        if self.ws_connection:
            await self.ws_connection.close()
        
        logger.info("Data pipeline cleaned up")

class MarketDataValidator:
    """Validate market data for consistency"""
    
    @staticmethod
    def validate_snapshot(snapshot: OrderBookSnapshot) -> bool:
        """Validate order book snapshot"""
        
        # Prices should be in [0, 1]
        if not (0 <= snapshot.yes_price <= 1):
            logger.warning(f"Invalid YES price: {snapshot.yes_price}")
            return False
        
        if not (0 <= snapshot.no_price <= 1):
            logger.warning(f"Invalid NO price: {snapshot.no_price}")
            return False
        
        # Liquidity should be non-negative
        if snapshot.yes_liquidity < 0 or snapshot.no_liquidity < 0:
            logger.warning(f"Invalid liquidity values")
            return False
        
        # Spread should be small and non-negative
        if snapshot.bid_ask_spread < 0 or snapshot.bid_ask_spread > 0.5:
            logger.warning(f"Invalid bid-ask spread: {snapshot.bid_ask_spread}")
            return False
        
        return True

class MarketDataCache:
    """Cache historical market data for analysis"""
    
    def __init__(self, max_history_size: int = 10000):
        self.max_size = max_history_size
        self.history: Dict[str, List[OrderBookSnapshot]] = {}
    
    def add_snapshot(self, snapshot: OrderBookSnapshot):
        """Add snapshot to history"""
        
        if snapshot.market_id not in self.history:
            self.history[snapshot.market_id] = []
        
        self.history[snapshot.market_id].append(snapshot)
        
        # Trim if too large
        if len(self.history[snapshot.market_id]) > self.max_size:
            self.history[snapshot.market_id] = self.history[snapshot.market_id][-self.max_size:]
    
    def get_history(self, market_id: str) -> List[OrderBookSnapshot]:
        """Get market history"""
        return self.history.get(market_id, [])
    
    def get_price_change(self, market_id: str) -> Optional[float]:
        """Get price change from first to last snapshot"""
        
        history = self.get_history(market_id)
        if len(history) < 2:
            return None
        
        first = history[0]
        last = history[-1]
        
        change = ((last.mid_price - first.mid_price) / first.mid_price) * 100
        return change
