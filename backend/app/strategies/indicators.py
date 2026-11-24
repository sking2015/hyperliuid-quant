from decimal import Decimal


class EMA:
    def __init__(self, period: int):
        if period < 1:
            raise ValueError("EMA period must be positive")
        self.period = period
        self.alpha = Decimal(2) / (period + 1)
        self.value = None
        self.count = 0
        self.total = Decimal(0)

    def update(self, price: Decimal) -> Decimal | None:
        if self.value is not None:
            self.value += self.alpha * (price - self.value)
        else:
            self.count += 1
            self.total += price
            if self.count == self.period:
                self.value = self.total / self.period
        return self.value

