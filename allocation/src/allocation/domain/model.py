"""Domain model for the allocation service.

Plain Python only: nothing in this module imports a web framework, an ORM or a database
driver. That is what lets the business rules be tested in milliseconds and reused unchanged
behind any entrypoint.
"""

from dataclasses import dataclass
from datetime import date


class OutOfStock(Exception):
    """Raised when no batch can take an order line."""

    pass


def allocate(line: "OrderLine", batches: list["Batch"]) -> str:
    """Allocate an order line to the best available batch.

    "Best" is decided by ``Batch.__gt__``: warehouse stock first, then the earliest ETA.

    Args:
        line: The order line to allocate.
        batches: Candidate batches. Batches for other SKUs are skipped by ``can_allocate``.

    Returns:
        The reference of the batch the line was allocated to.

    Raises:
        OutOfStock: If no batch can take the line.
    """
    try:
        # sorted() uses Batch.__gt__, so the first batch that fits is the preferred one.
        batch = next(b for b in sorted(batches) if b.can_allocate(line))
    except StopIteration:
        # `from None` hides the internal StopIteration from the traceback.
        raise OutOfStock(f"Out of stock for sku {line.sku}") from None
    batch.allocate(line)
    return batch.reference


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
    sku: str
    qty: int


class Batch:
    """A quantity of one SKU bought from a supplier, either in the warehouse or on its way.

    An entity: identified by its ``reference``, whatever its current quantity or allocations.

    Args:
        ref: Unique reference of this batch.
        sku: The product this batch contains.
        qty: Quantity purchased. The quantity still available is derived, never stored.
        eta: Arrival date for a shipment, or ``None`` for stock already in the warehouse.
    """

    def __init__(self, ref: str, sku: str, qty: int, eta: date | None):
        self.reference = ref
        self.sku = sku
        self.eta = eta
        self._purchased_quantity = qty
        # A set, so allocating an equal line twice is a no-op: allocation is idempotent.
        self._allocations: set[OrderLine] = set()

    def __repr__(self):
        return f"<Batch {self.reference}>"

    def __eq__(self, other):
        """Batches are equal when their reference is equal, regardless of their state."""
        if not isinstance(other, Batch):
            return False
        return other.reference == self.reference

    def __hash__(self):
        """Hash by reference only, so it stays consistent with ``__eq__``."""
        return hash(self.reference)

    def __gt__(self, other):
        """Order batches by how soon their stock can ship.

        Warehouse stock (``eta is None``) sorts first, then the earliest ETA. This is how
        ``sorted(batches)`` expresses the business preference rule.
        """
        if self.eta is None:
            return False
        if other.eta is None:
            return True
        return self.eta > other.eta

    def allocate(self, line: OrderLine):
        """Reserve ``line`` in this batch.

        Does nothing if the line does not fit or is already allocated, so repeating a request
        (a double submit, a retry) is harmless.
        """
        if self.can_allocate(line):
            self._allocations.add(line)

    def deallocate(self, line: OrderLine):
        """Release a previously allocated line. Lines that were never allocated are ignored."""
        if line in self._allocations:
            self._allocations.remove(line)

    @property
    def allocated_quantity(self) -> int:
        """Total quantity of the lines allocated to this batch."""
        return sum(line.qty for line in self._allocations)

    @property
    def available_quantity(self) -> int:
        """Purchased quantity minus allocated quantity.

        Computed on demand, so it can never drift out of sync with the allocations.
        """
        return self._purchased_quantity - self.allocated_quantity

    def can_allocate(self, line: OrderLine) -> bool:
        """Return whether ``line`` is for this batch's SKU and fits in the available quantity."""
        return self.sku == line.sku and self.available_quantity >= line.qty
