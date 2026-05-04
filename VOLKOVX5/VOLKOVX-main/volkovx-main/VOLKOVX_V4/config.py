"""
VOLKOVX Configuration - All Tunable Parameters
Research-backed defaults from $39.7M extraction study

File Organization:
  1. System Configuration (API keys, timeouts)
  2. Arbitrage Detection Parameters (thresholds, constraints)
  3. Optimization Parameters (Frank-Wolfe settings)
  4. Position Sizing (Kelly, allocation limits)
  5. Execution Parameters (slippage, latency)
  6. Monitoring & Logging
"""

import os
import logging
from typing import Optional

# ============================================================================
# SYSTEM CONFIGURATION
# ============================================================================

class SystemConfig:
    """System-level settings"""
    
    # Environment
    ENVIRONMENT = os.getenv("ENVIRONMENT", "production")
    DEBUG_MODE = ENVIRONMENT == "debug"
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
    
    # Endpoints
    POLYMARKET_API_URL = os.getenv(
        "POLYMARKET_API_URL",
        "https://clob.polymarket.com/api"
    )
    POLYMARKET_WS_URL = os.getenv(
        "POLYMARKET_WS_URL",
        "wss://ws-api.polymarket.com"
    )
    POLYMARKET_API_KEY = os.getenv("POLYMARKET_API_KEY", "")
    
    ALCHEMY_POLYGON_RPC = os.getenv(
        "ALCHEMY_POLYGON_RPC",
        "https://polygon-mainnet.g.alchemy.com/v2/KEY"
    )
    
    # Monitoring
    ENABLE_METRICS = os.getenv("ENABLE_METRICS", "true").lower() == "true"
    METRICS_PORT = int(os.getenv("METRICS_PORT", "8000"))

# ============================================================================
# PORTFOLIO & RISK MANAGEMENT
# ============================================================================

class PortfolioConfig:
    """Portfolio settings"""
    
    # Capital
    PORTFOLIO_VALUE = float(os.getenv("PORTFOLIO_VALUE", "100000"))
    
    # Allocation limits
    MAX_PORTFOLIO_ALLOCATION = float(os.getenv(
        "MAX_PORTFOLIO_ALLOCATION",
        "0.05"  # 5% per trade
    ))
    
    MAX_CONCURRENT_POSITIONS = int(os.getenv(
        "MAX_CONCURRENT_POSITIONS",
        "20"
    ))
    
    # Daily/session limits
    MAX_DAILY_LOSS = float(os.getenv(
        "MAX_DAILY_LOSS",
        "0.10"  # 10% of portfolio
    ))
    
    MAX_DAILY_TRADES = int(os.getenv(
        "MAX_DAILY_TRADES",
        "1000"
    ))

# ============================================================================
# ARBITRAGE DETECTION PARAMETERS
# ============================================================================

class ArbitrageConfig:
    """Arbitrage detection thresholds"""
    
    # Minimum profit threshold (research: $0.60 median mispricing)
    MIN_PROFIT_THRESHOLD = float(os.getenv(
        "MIN_PROFIT_THRESHOLD",
        "0.001"  # $0.001 minimum
    ))
    
    # Minimum margin (% profit)
    MIN_PROFIT_MARGIN_PERCENT = float(os.getenv(
        "MIN_PROFIT_MARGIN_PERCENT",
        "0.5"  # 0.5% of position
    ))
    
    # Market filter
    MIN_LIQUIDITY = float(os.getenv(
        "MIN_LIQUIDITY",
        "5000"  # $5000 per side
    ))
    
    MAX_BID_ASK_SPREAD = float(os.getenv(
        "MAX_BID_ASK_SPREAD",
        "0.20"  # 20% spread too wide
    ))
    
    # Constraint detection
    ENABLE_MULTI_MARKET_DETECTION = os.getenv(
        "ENABLE_MULTI_MARKET_DETECTION",
        "true"
    ).lower() == "true"
    
    MULTI_MARKET_DEPTH = int(os.getenv(
        "MULTI_MARKET_DEPTH",
        "3"  # Check up to 3 markets for dependencies
    ))

# ============================================================================
# OPTIMIZATION PARAMETERS (FRANK-WOLFE)
# ============================================================================

class OptimizationConfig:
    """Multi-market optimization settings"""
    
    # Frank-Wolfe algorithm
    FRANK_WOLFE_MAX_ITERATIONS = int(os.getenv(
        "FRANK_WOLFE_MAX_ITERATIONS",
        "150"  # Research: 50-150 typical
    ))
    
    FRANK_WOLFE_CONVERGENCE_GAP = float(os.getenv(
        "FRANK_WOLFE_CONVERGENCE_GAP",
        "0.001"  # Stop when gap < 0.1%
    ))
    
    FRANK_WOLFE_TIMEOUT_SECONDS = float(os.getenv(
        "FRANK_WOLFE_TIMEOUT_SECONDS",
        "30"  # 30 second maximum
    ))
    
    # Initial active set size
    INITIAL_ACTIVE_SET_SIZE = int(os.getenv(
        "INITIAL_ACTIVE_SET_SIZE",
        "5"
    ))

# ============================================================================
# POSITION SIZING (KELLY CRITERION)
# ============================================================================

class PositionSizingConfig:
    """Kelly criterion and position sizing"""
    
    # Kelly fraction
    KELLY_FRACTION = float(os.getenv(
        "KELLY_FRACTION",
        "0.25"  # 1/4 Kelly for stability
    ))
    
    # Allocation limits
    MAX_PORTFOLIO_ALLOCATION = float(os.getenv(
        "MAX_PORTFOLIO_ALLOCATION",
        "0.05"  # 5%
    ))
    
    # Order book constraints
    ORDER_BOOK_DEPTH_MAX = float(os.getenv(
        "ORDER_BOOK_DEPTH_MAX",
        "0.30"  # Don't use >30% of depth
    ))
    
    # Execution risk
    EXPECTED_EXECUTION_RISK = float(os.getenv(
        "EXPECTED_EXECUTION_RISK",
        "0.02"  # 2% chance of failed execution
    ))
    
    # Slippage
    EXPECTED_SLIPPAGE = float(os.getenv(
        "EXPECTED_SLIPPAGE",
        "0.001"  # 0.1%
    ))

# ============================================================================
# EXECUTION PARAMETERS
# ============================================================================

class ExecutionConfig:
    """Trade execution settings"""
    
    # Latency tolerance
    MAX_EXECUTION_LATENCY_MS = int(os.getenv(
        "MAX_EXECUTION_LATENCY_MS",
        "50"  # <50ms target
    ))
    
    # Order submission
    ORDER_SUBMISSION_TIMEOUT_SECONDS = float(os.getenv(
        "ORDER_SUBMISSION_TIMEOUT_SECONDS",
        "5.0"
    ))
    
    # Retry settings
    MAX_SUBMISSION_RETRIES = int(os.getenv(
        "MAX_SUBMISSION_RETRIES",
        "3"
    ))
    
    RETRY_BACKOFF_MS = int(os.getenv(
        "RETRY_BACKOFF_MS",
        "100"
    ))
    
    # Block-level execution
    ATOMIC_EXECUTION = os.getenv(
        "ATOMIC_EXECUTION",
        "true"
    ).lower() == "true"

# ============================================================================
# DATA PIPELINE
# ============================================================================

class DataPipelineConfig:
    """Market data settings"""
    
    # WebSocket
    WEBSOCKET_BUFFER_SIZE = int(os.getenv(
        "WEBSOCKET_BUFFER_SIZE",
        "1000"
    ))
    
    WEBSOCKET_HEARTBEAT_SECONDS = int(os.getenv(
        "WEBSOCKET_HEARTBEAT_SECONDS",
        "30"
    ))
    
    # Update frequency
    ORDER_BOOK_UPDATE_INTERVAL_MS = int(os.getenv(
        "ORDER_BOOK_UPDATE_INTERVAL_MS",
        "100"  # 10 updates/second
    ))
    
    # Historical data
    MARKET_HISTORY_SIZE = int(os.getenv(
        "MARKET_HISTORY_SIZE",
        "10000"  # Keep 10k snapshots
    ))
    
    # On-chain confirmation
    CONFIRMATION_BLOCKS = int(os.getenv(
        "CONFIRMATION_BLOCKS",
        "12"  # 12 block confirmations
    ))

# ============================================================================
# MONITORING & LOGGING
# ============================================================================

class MonitoringConfig:
    """System monitoring"""
    
    # Performance metrics
    TRACK_PERFORMANCE_METRICS = True
    METRICS_REPORTING_INTERVAL = int(os.getenv(
        "METRICS_REPORTING_INTERVAL",
        "60"  # Report every 60 seconds
    ))
    
    # Logging
    LOG_TO_FILE = os.getenv("LOG_TO_FILE", "true").lower() == "true"
    LOG_FILE_PATH = os.getenv("LOG_FILE_PATH", "./volkovx.log")
    LOG_FILE_MAX_BYTES = int(os.getenv("LOG_FILE_MAX_BYTES", "104857600"))
    LOG_FILE_BACKUP_COUNT = int(os.getenv("LOG_FILE_BACKUP_COUNT", "10"))
    
    # Alert thresholds
    ALERT_ON_LARGE_LOSS = True
    LARGE_LOSS_THRESHOLD = float(os.getenv(
        "LARGE_LOSS_THRESHOLD",
        "0.05"  # Alert on 5% loss
    ))

# ============================================================================
# CONFIGURATION VALIDATION & ACCESS
# ============================================================================

def validate_config():
    """Validate all configuration parameters"""
    
    errors = []
    
    # Check required API keys
    if not SystemConfig.POLYMARKET_API_KEY and not SystemConfig.ENVIRONMENT == "debug":
        errors.append("POLYMARKET_API_KEY not set")
    
    # Check portfolio value
    if PortfolioConfig.PORTFOLIO_VALUE <= 0:
        errors.append("PORTFOLIO_VALUE must be positive")
    
    # Check allocation limits
    if PositionSizingConfig.MAX_PORTFOLIO_ALLOCATION <= 0:
        errors.append("MAX_PORTFOLIO_ALLOCATION must be positive")
    
    if PositionSizingConfig.KELLY_FRACTION <= 0 or PositionSizingConfig.KELLY_FRACTION > 1:
        errors.append("KELLY_FRACTION must be in (0, 1]")
    
    # Check arbitrage thresholds
    if ArbitrageConfig.MIN_PROFIT_THRESHOLD < 0:
        errors.append("MIN_PROFIT_THRESHOLD must be non-negative")
    
    # Check optimization
    if OptimizationConfig.FRANK_WOLFE_MAX_ITERATIONS <= 0:
        errors.append("FRANK_WOLFE_MAX_ITERATIONS must be positive")
    
    if OptimizationConfig.FRANK_WOLFE_TIMEOUT_SECONDS <= 0:
        errors.append("FRANK_WOLFE_TIMEOUT_SECONDS must be positive")
    
    if errors:
        raise ValueError("Configuration errors:\n" + "\n".join(errors))
    
    logging.info("✅ Configuration validated")

def get_config():
    """
    Get unified configuration object
    
    Returns:
        Object with all configuration parameters
    """
    
    class UnifiedConfig:
        # System
        ENVIRONMENT = SystemConfig.ENVIRONMENT
        DEBUG_MODE = SystemConfig.DEBUG_MODE
        LOG_LEVEL = SystemConfig.LOG_LEVEL
        POLYMARKET_API_URL = SystemConfig.POLYMARKET_API_URL
        POLYMARKET_WS_URL = SystemConfig.POLYMARKET_WS_URL
        POLYMARKET_API_KEY = SystemConfig.POLYMARKET_API_KEY
        ALCHEMY_POLYGON_RPC = SystemConfig.ALCHEMY_POLYGON_RPC
        
        # Portfolio
        PORTFOLIO_VALUE = PortfolioConfig.PORTFOLIO_VALUE
        MAX_PORTFOLIO_ALLOCATION = PortfolioConfig.MAX_PORTFOLIO_ALLOCATION
        MAX_CONCURRENT_POSITIONS = PortfolioConfig.MAX_CONCURRENT_POSITIONS
        
        # Arbitrage
        MIN_PROFIT_THRESHOLD = ArbitrageConfig.MIN_PROFIT_THRESHOLD
        MIN_PROFIT_MARGIN_PERCENT = ArbitrageConfig.MIN_PROFIT_MARGIN_PERCENT
        MIN_LIQUIDITY = ArbitrageConfig.MIN_LIQUIDITY
        MAX_BID_ASK_SPREAD = ArbitrageConfig.MAX_BID_ASK_SPREAD
        ENABLE_MULTI_MARKET_DETECTION = ArbitrageConfig.ENABLE_MULTI_MARKET_DETECTION
        
        # Optimization
        FRANK_WOLFE_MAX_ITERATIONS = OptimizationConfig.FRANK_WOLFE_MAX_ITERATIONS
        FRANK_WOLFE_CONVERGENCE_GAP = OptimizationConfig.FRANK_WOLFE_CONVERGENCE_GAP
        FRANK_WOLFE_TIMEOUT_SECONDS = OptimizationConfig.FRANK_WOLFE_TIMEOUT_SECONDS
        
        # Position Sizing
        KELLY_FRACTION = PositionSizingConfig.KELLY_FRACTION
        ORDER_BOOK_DEPTH_MAX = PositionSizingConfig.ORDER_BOOK_DEPTH_MAX
        
        # Execution
        MAX_EXECUTION_LATENCY_MS = ExecutionConfig.MAX_EXECUTION_LATENCY_MS
        ATOMIC_EXECUTION = ExecutionConfig.ATOMIC_EXECUTION
    
    return UnifiedConfig()

# ============================================================================
# LOGGING SETUP
# ============================================================================

def setup_logging():
    """Configure logging system"""
    
    logger = logging.getLogger("VOLKOVX")
    
    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(SystemConfig.LOG_LEVEL)
    
    # Formatter
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    console_handler.setFormatter(formatter)
    
    logger.addHandler(console_handler)
    logger.setLevel(SystemConfig.LOG_LEVEL)
    
    return logger
