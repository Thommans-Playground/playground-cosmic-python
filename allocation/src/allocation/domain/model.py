"""Domain model for the allocation service.

Plain Python only: nothing in this module imports a web framework, an ORM or a database
driver. That is what lets the business rules be tested in milliseconds and reused unchanged
behind any entrypoint.
"""

from dataclasses import dataclass


class OutOfStock(Exception):
    """Raised when no batch can take an order line."""

    pass


def allocate():
    pass


@dataclass(unsafe_hash=True)
class OrderLine:
    """One line of a customer order: a quantity of one SKU.

    A value object: two lines with the same fields are the same line. Treat it as immutable.
    It is hashable so that a ``Batch`` can keep its allocated lines in a ``set``.

    Attributes:
        orderid: Identifier of the customer order this line belongs to.
        sku: Stock-keeping unit: the product being ordered.
        qty: How many units are wanted.
    """

    orderid: str


class Batch:
    """A quantity of one SKU bought from a supplier, either in the warehouse or on its way.

    An entity: identified by its ``reference``, whatever its current quantity or allocations.

    Args:
        ref: Unique reference of this batch.
        sku: The product this batch contains.
        qty: Quantity purchased. The quantity still available is derived, never stored.
        eta: Arrival date for a shipment, or ``None`` for stock already in the warehouse.
    """

    def __init__(self):
        pass
