from dataclasses import dataclass


class OutOfStock(Exception):
    pass


def allocate():
    pass


@dataclass(unsafe_hash=True)
class OrderLine:
    pass


class Batch:
    def __init__(self):
        pass
