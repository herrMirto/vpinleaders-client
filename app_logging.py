import logging
import os
import sys
from logging.handlers import RotatingFileHandler


DEFAULT_LOG_FILE = os.path.join(
    os.path.expanduser('~'),
    '.vpinleaders',
    'logs',
    'vpinleaders.log',
)

LOG_FORMAT = '%(asctime)s.%(msecs)03d %(levelname)s  [%(name)s] %(message)s'
DATE_FORMAT = '%Y-%m-%d %H:%M:%S'


def default_log_file():
    return DEFAULT_LOG_FILE


def get_logger(name):
    return logging.getLogger(name)


def log_message(logger, level, msg):
    level_name = str(level or 'INFO').upper()
    logger.log(getattr(logging, level_name, logging.INFO), msg)


def configure_logging(log_file=None, console=True, level='INFO'):
    logging.addLevelName(logging.WARNING, 'WARN')

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    resolved_level = getattr(logging, str(level or 'INFO').upper(), logging.INFO)
    root.setLevel(resolved_level)

    formatter = logging.Formatter(LOG_FORMAT, DATE_FORMAT)
    handlers = []

    if console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        console_handler.setLevel(resolved_level)
        handlers.append(console_handler)

    actual_log_file = None
    if log_file:
        try:
            actual_log_file = os.path.abspath(os.path.expanduser(log_file))
            os.makedirs(os.path.dirname(actual_log_file), exist_ok=True)
            file_handler = RotatingFileHandler(
                actual_log_file,
                maxBytes=5 * 1024 * 1024,
                backupCount=5,
                encoding='utf-8',
            )
            file_handler.setFormatter(formatter)
            file_handler.setLevel(resolved_level)
            handlers.append(file_handler)
        except Exception as exc:
            fallback = logging.StreamHandler(sys.stdout)
            fallback.setFormatter(formatter)
            fallback.setLevel(resolved_level)
            handlers.append(fallback)
            actual_log_file = None
            root.addHandler(fallback)
            root.error('Failed to initialize file logging at %s: %s', log_file, exc)
            return actual_log_file

    if not handlers:
        null_handler = logging.NullHandler()
        handlers.append(null_handler)

    for handler in handlers:
        root.addHandler(handler)

    return actual_log_file
