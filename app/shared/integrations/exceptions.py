class IntegrationError(Exception):
    """Integration failure exposed to callers instead of SDK-specific exceptions."""


class IntegrationConfigurationError(IntegrationError):
    """Invalid integration configuration detected at composition time."""
