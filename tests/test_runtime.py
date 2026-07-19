"""Live backend runtime ownership invariants."""

from mcp_gateway import runtime


def test_backend_runtime_keeps_proxy_and_transform_lifetimes_paired():
    backend_runtime = runtime.BackendRuntime()
    proxy = object()
    initial = [object()]
    replacement = [object(), object()]

    backend_runtime.mount("backend", proxy, initial)
    assert backend_runtime.get_proxy("backend") is proxy
    assert backend_runtime.get_transforms("backend") == initial

    backend_runtime.replace_transforms("backend", replacement)
    assert backend_runtime.get_transforms("backend") == replacement

    backend_runtime.unmount("backend")
    assert backend_runtime.get_proxy("backend") is None
    assert backend_runtime.get_transforms("backend") == []
