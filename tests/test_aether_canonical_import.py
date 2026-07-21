def test_canonical_aether_imports_aether_next_kernel() -> None:
    import aether
    from aether.compiler import ConfigCompiler
    from aether.kernel import AetherNextKernel

    assert aether.AetherNextKernel is AetherNextKernel
    assert ConfigCompiler.__name__ == "ConfigCompiler"
