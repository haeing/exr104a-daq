"""Open a scope resource and retain useful connection failure details."""


def open_instrument(manager, ip, resource=None):
    address = resource or f"TCPIP0::{ip}::inst0::INSTR"
    try:
        return manager.open_resource(address)
    except Exception as exc:
        failures = [f"{address}: {exc}"]
        # A default VXI-11 service may be unavailable while raw SCPI is enabled.
        # An explicit user resource is always respected, without fallback.
        if resource is None and getattr(exc, "error_code", None) == -1073807343:
            socket_address = f"TCPIP0::{ip}::5025::SOCKET"
            try:
                return manager.open_resource(socket_address)
            except Exception as socket_exc:
                failures.append(f"{socket_address}: {socket_exc}")
        raise RuntimeError(
            "Could not open scope connection. " + " | ".join(failures)
            + ". Check the scope IP, remote-control service, and other active acquisition programs."
        ) from exc
