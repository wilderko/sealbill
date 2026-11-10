"""The issuing organization on a client page whose view does not pass it itself (the /k/ pages)."""


def issuer(request) -> dict:
    return dict(getattr(request, "client_identity", None) or {})
