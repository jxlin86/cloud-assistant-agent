"""Additional field names used by Azure, Google Cloud, and AWS credentials."""
import re
from fde_oci_agent.audit import Redactor as CoreRedactor, SENSITIVE_KEY


class Redactor(CoreRedactor):
    def clean(self, value):
        if isinstance(value, dict):
            return {str(key): '[REDACTED]' if SENSITIVE_KEY.search(str(key)) or re.search(
                r'connection.?string|account.?key|access.?key|shared.?access.?signature|credential', str(key), re.I
            ) else self.clean(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.clean(item) for item in value]
        return super().clean(value)
