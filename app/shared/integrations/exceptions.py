class IntegrationError(Exception):
    """Failure in an integration's own response contract."""


class IntegrationConfigurationError(IntegrationError):
    """Invalid integration configuration detected at composition time."""
