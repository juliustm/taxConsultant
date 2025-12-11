from datetime import datetime

class Receipt:
    def __init__(self, vendor, amount, description, receipt_number=None, date=None):
        self.vendor = vendor
        self.amount = amount
        self.description = description
        self.receipt_number = receipt_number
        # Parse date string if provided, otherwise use current time
        if isinstance(date, str):
            try:
                self.date = datetime.fromisoformat(date)
            except ValueError:
                self.date = datetime.now()
        else:
            self.date = date if date else datetime.now()
        self.logged_at = datetime.now()

    def to_dict(self):
        return {
            'vendor': self.vendor,
            'amount': self.amount,
            'description': self.description,
            'receipt_number': self.receipt_number,
            'date': self.date.isoformat(),
            'logged_at': self.logged_at.isoformat()
        }

    @staticmethod
    def log_receipt(vendor, amount, description, receipt_number=None, date=None):
        """
        Create and log a new receipt
        """
        receipt = Receipt(
            vendor=vendor,
            amount=amount,
            description=description,
            receipt_number=receipt_number,
            date=date
        )
        # TODO: In future iterations, we can add database storage here
        return receipt
