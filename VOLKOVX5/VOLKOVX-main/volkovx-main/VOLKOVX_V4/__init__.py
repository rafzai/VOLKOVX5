"""
VOLKOVX_V4 - Package initialization
"""

__version__ = "4.0.0"
__author__ = "VOLKOVX Team"
__description__ = "Guaranteed Arbitrage Engine for Polymarket Prediction Markets"

from VOLKOVX_V4.engine import VolkovxArbitrageEngine
from VOLKOVX_V4.bregman_projection import BregmanProjectionEngine, ArbitrageOpportunity
from VOLKOVX_V4.frank_wolfe_solver import FrankWolfeSolver
from VOLKOVX_V4.position_sizing import PositionSizingEngine
from VOLKOVX_V4.data_pipeline import DataPipeline
from VOLKOVX_V4.config import get_config

__all__ = [
    'VolkovxArbitrageEngine',
    'BregmanProjectionEngine',
    'ArbitrageOpportunity',
    'FrankWolfeSolver',
    'PositionSizingEngine',
    'DataPipeline',
    'get_config',
]
