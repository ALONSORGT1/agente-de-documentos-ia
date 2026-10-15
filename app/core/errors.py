"""Errores públicos controlados. No incluir secretos ni respuestas crudas de APIs."""


class ConfigurationError(ValueError):
    pass


class DocumentError(ValueError):
    pass


class StateError(RuntimeError):
    pass


class BusyError(RuntimeError):
    pass


class ProviderError(RuntimeError):
    pass
