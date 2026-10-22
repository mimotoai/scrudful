from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("scrudful")
except PackageNotFoundError:  # a source tree that isn't installed
    __version__ = "0+unknown"
