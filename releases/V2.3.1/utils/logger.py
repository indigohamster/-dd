# ============================================================
# 日志配置
# ============================================================

import logging
import sys


def setup_logger(
    name: str = "dingtalk-bot",
    level: str = "INFO",
    fmt: str = "%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    datefmt: str = "%Y-%m-%d %H:%M:%S",
) -> logging.Logger:
    """
    创建标准化的 Logger 实例。

    Args:
        name:   Logger 名称
        level:  日志级别 (DEBUG / INFO / WARNING / ERROR)
        fmt:    日志格式
        datefmt: 时间格式

    Returns:
        配置好的 Logger
    """
    logger = logging.getLogger(name)

    # 避免重复添加 Handler
    if logger.handlers:
        return logger

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
    logger.addHandler(handler)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    return logger


# 兼容别名
def get_logger(name: str, level: str = "INFO") -> logging.Logger:
    """get_logger 是 setup_logger 的别名，供其他模块使用"""
    return setup_logger(name=name, level=level)
