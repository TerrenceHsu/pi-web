"""Transforms see the current binding without changing legacy signal dispatch."""
from __future__ import annotations

import asyncio

from pi_agent_core_py import FakeClient
from pi_agent_core_py.agent.context import ContextTransformInfo, apply_transform_context


async def test_transform_receives_actual_binding_and_signal():
    signal = asyncio.Event()
    info = ContextTransformInfo("actual prompt", (), FakeClient([]), 3)
    seen = []

    async def transform(messages, signal=None, *, model_context=None):
        seen.append((signal, model_context))
        return messages

    assert await apply_transform_context(transform, [], signal, model_context=info) == []
    assert seen == [(signal, info)]


async def test_transform_context_only_argument_does_not_receive_duplicate_signal():
    info = ContextTransformInfo("prompt", (), FakeClient([]), 1)
    seen = []

    def transform(messages, model_context=None):
        seen.append(model_context)
        return messages

    await apply_transform_context(transform, [], model_context=info)
    assert seen == [info]


async def test_legacy_kwargs_do_not_gain_model_binding():
    seen = []

    def transform(messages, **kwargs):
        seen.append(kwargs)
        return messages

    signal = asyncio.Event()
    await apply_transform_context(transform, [], signal)
    assert seen == [{"signal": signal}]
