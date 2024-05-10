__all__ = [
    "DrmsError",
    "DrmsQueryError",
    "DrmsExportError",
    "DrmsOperationNotSupported",
    "DrmsLoginFailure",
]


class DrmsError(RuntimeError):
    """
    Unspecified DRMS run-time error.
    """


class DrmsQueryError(DrmsError):
    """
    DRMS query error.
    """


class DrmsExportError(DrmsError):
    """
    DRMS data export error.
    """


class DrmsOperationNotSupported(DrmsError):
    """
    Operation is not supported by DRMS server.
    """

class DrmsLoginFailure(DrmsError):
    """
    Failure logging in with provided email address and password.
    """