"""
FinPlus+ Core Logging Facility
Institutional Standard: ISO-8601 timestamps, structured levels, zero console clutter.
"""

import logging
import sys
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))

class FinPlusFormatter(logging.Formatter):
    """Custom formatter providing ISO-8601 timestamps with IST offset and FinPlus+ identity."""
    
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=IST)
        return dt.isoformat(timespec="seconds")

def get_finplus_logger(name: str = "FinPlus", level: int = logging.INFO) -> logging.Logger:
    """Creates or returns an institutional standard logger for FinPlus+ applications."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.setLevel(level)
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(level)
        formatter = FinPlusFormatter(
            fmt="[%(asctime)s] [%(levelname)s] [FinPlus+] [%(name)s] %(message)s"
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.propagate = False
    return logger

