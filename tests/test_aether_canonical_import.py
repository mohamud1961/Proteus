def test_canonical_aether_imports_aether_next_kernel() -> None:
    import older_variants.aether as aether
    from proteus.aether_next.compiler import ConfigCompiler
    from proteus.aether_next.kernel import AetherNextKernel

    assert aether.AetherNextKernel is AetherNextKernel
    assert ConfigCompiler.__name__ == "ConfigCompiler"
