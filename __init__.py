"""Hermes-OmO RPC bridge plugin."""


def register(ctx):
    from . import tools
    tools.register(ctx)
